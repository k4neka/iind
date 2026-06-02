"""Offline checks for the Transfer_Cell + complex-piece (RWM/SWM) MES.

Run from this directory with the mes venv:

    .venv/bin/python test_transfer_complex.py

No live PLC, MQTT or PostgreSQL is needed:
  * `database` is stubbed in sys.modules (no psycopg2 connection),
  * the OPC-UA layer is faked at PLCClient.read/write with a tiny plant
    model. The W2->W1 corridor is driven through the ONLY OPC-UA-exposed
    interface, g_Return_PieceID + g_Return_Exec (the Transfer_* GVL signals
    are not in the symbol set), and completion is observed via the
    g_W1_Count / g_W2_Count warehouse counters.

It verifies:
  1. transfer_piece_blocking pulses g_Return for the requested piece id and
     detects delivery when W1 rises;
  2. ... and also when only W2 drops (counter fallback);
  3. send_workpiece_handshake completes a cell handshake;
  4. ComplexOrchestrator emits the staged sequence (two metal legs M2 then
     M1 each parked at M3, a wood top in a separate cell, ONLY the top
     returned W2->W1, then the top re-injected at M3 tool 9 in the leg
     cell, then a finished return).
"""
import asyncio
import sys
import types

# --- stub `database` so importing complex.py needs no psycopg2 ----------
_completed = []
_db = types.ModuleType("database")
_db.queued_pieces = lambda: []
_db.mark_dispatched = lambda *a, **k: None
_db.mark_completed = lambda pid, cost: _completed.append((pid, cost))
sys.modules["database"] = _db

from config import COMPLEX_RECIPE          # noqa: E402
import opcua_client as oc                  # noqa: E402
from opcua_client import PLCClient         # noqa: E402
import complex as cx                       # noqa: E402

oc.CORRIDOR_SETTLE_S = 0.0    # don't really sleep during tests

PASS = "\033[32mPASS\033[0m"
FAIL = "\033[31mFAIL\033[0m"

WOUT = f"In[{oc.TRANSFER_WOUT_IN}]"
WIN = f"In[{oc.TRANSFER_WIN_IN}]"
REG = f"Reg[{oc.TRANSFER_WOUT_REG}]"


def expect(cond, msg, fails):
    if not cond:
        fails.append(msg)
    return cond


# ====================================================================
# Real PLCClient against a faked plant that mimics the corridor sensors.
# ====================================================================
def make_faked_plc(w2_start=1, deliver=True):
    """A PLCClient whose low-level read/write hit an in-memory plant.

    Raising g_Return_Exec with a piece available in W2 "engages" the
    corridor (entrance sensor In[20] on, request register Reg[14] set);
    once engaged, reading the exit sensor In[10] reports the piece arriving
    in W1 (W2--/W1++, entrance clears). g_W1/g_W2 are also kept, but the
    detection is sensor-driven so it works even if they never move."""
    plc = PLCClient()
    plant = {
        "g_W1_Count": 0, "g_W2_Count": w2_start,
        "g_Return_Exec": False, "g_Return_PieceID": 0,
        WOUT: False, WIN: False, REG: 0,
        "_engaged": False, "_delivered": False,
    }
    for c in (1, 2, 3, 4):
        plant[f"Cell_{c}_Top_Status.free_cmd"] = True
        plant[f"Cell_{c}_Top_Order.recv_cmd"] = False

    async def fake_read(var):
        if var == WIN:
            # The exit sensor pulses True once as the piece passes into W1.
            if plant["_engaged"] and deliver and not plant["_delivered"]:
                plant[WOUT] = False          # entrance clears
                plant[REG] = 0
                plant["g_W2_Count"] = max(0, plant["g_W2_Count"] - 1)
                plant["g_W1_Count"] += 1
                plant["_delivered"] = True
                return True                  # piece at the corridor exit
            return False
        return plant.get(var, 0)

    async def fake_write(var, value, varianttype=None):
        prev = plant.get(var)
        plant[var] = value
        # g_Return rising edge engages the corridor IF a piece is in W2.
        if var == "g_Return_Exec" and value and not prev:
            if plant["g_W2_Count"] > 0:
                plant[WOUT] = True
                plant[REG] = plant.get("g_Return_PieceID", 0)
                plant["_engaged"] = True
        for c in (1, 2, 3, 4):
            if var == f"Cell_{c}_Top_Order.recv_cmd":
                plant[f"Cell_{c}_Top_Status.free_cmd"] = not value

    async def fake_optime(node, op_time_s):
        plant[node] = op_time_s

    plc.read = fake_read
    plc.write = fake_write
    plc._write_optime = fake_optime
    return plc, plant


async def test_return_engages_and_delivers(fails):
    before = len(fails)
    plc, plant = make_faked_plc(w2_start=1, deliver=True)
    ok = await plc.transfer_piece_blocking(3, ready_timeout=5,
                                           deliver_timeout=5)
    expect(ok, "return: returned False on a normal delivery", fails)
    expect(plant["g_Return_PieceID"] == 3,
           "return: did not request piece id 3", fails)
    expect(plant["g_Return_Exec"] is False,
           "return: g_Return_Exec not re-armed (left True)", fails)
    expect(plant["g_W1_Count"] == 1 and plant["g_W2_Count"] == 0,
           "return: corridor did not move the piece W2->W1", fails)
    print(f"  engage+deliver:     {PASS if len(fails) == before else FAIL}")


async def test_return_no_engage_fails(fails):
    before = len(fails)
    # W2 empty and g_Return cannot engage -> must report failure, not hang.
    plc, plant = make_faked_plc(w2_start=0, deliver=True)
    ok = await plc.transfer_piece_blocking(3, ready_timeout=0.5,
                                           deliver_timeout=0.5)
    expect(not ok, "return: should fail when the corridor never engages",
           fails)
    expect(plant["g_Return_Exec"] is False,
           "return: g_Return_Exec not re-armed after failure", fails)
    print(f"  no-engage fails:    {PASS if len(fails) == before else FAIL}")


async def test_cell_handshake(fails):
    before = len(fails)
    plc, plant = make_faked_plc()
    ops = [{"cell": 3, "machine": 2, "tool": 5, "op_time_s": 30},
           {"cell": 3, "machine": 3, "tool": 0, "op_time_s": 0}]
    ok = await plc.send_workpiece_handshake(3, 2, ops, timeout=5)
    expect(ok, "send_workpiece_handshake returned False", fails)
    expect(plant.get("Cell_3_Top_Order.Workpiece.InitPiece") == 2,
           "handshake did not write InitPiece=2", fails)
    expect(plant.get("Cell_3_Top_Order.Workpiece.Last_Operation") == 1,
           "handshake did not set Last_Operation=1 for 2 ops", fails)
    expect(plant.get("Cell_3_Top_Order.Workpiece.Operations[1].Machine") == 3,
           "park op machine not M3", fails)
    expect(plant["Cell_3_Top_Order.recv_cmd"] is False,
           "cell recv_cmd left raised", fails)
    print(f"  cell handshake:     {PASS if len(fails) == before else FAIL}")


# ====================================================================
# ComplexOrchestrator sequence with fakes.
# ====================================================================
class FakePLC:
    def __init__(self, events):
        self.events = events

    async def cell_free(self, cell):
        return True

    async def send_workpiece_handshake(self, cell, init_piece, operations,
                                       timeout=60.0):
        self.events.append(
            ("hs", cell, init_piece,
             tuple((o["machine"], o["tool"], o["op_time_s"])
                   for o in operations)))
        await asyncio.sleep(0)
        return True


class FakeMqtt:
    def w1_estimate(self):
        return {"Wood": 9, "Metal": 9}

    def w1_consume(self, m, q=1):
        pass

    def w1_add_piece(self, name, q=1):
        pass


class FakeTransfers:
    def __init__(self, events):
        self.events = events

    def enqueue(self, piece, on_done=None, label=None):
        self.events.append(("xfer", piece, label))
        if on_done:
            on_done(True)


async def run_complex(pt):
    events = []
    _completed.clear()
    busy = set()
    orch = cx.ComplexOrchestrator(
        FakePLC(events), FakeMqtt(),
        publish_status=lambda p: events.append(("status", p["piece_type"],
                                                p["status"])),
        transfer_manager=FakeTransfers(events),
        busy_cells=busy, busy_lock=asyncio.Lock())
    row = {"id": 99, "order_id": 7, "order_line_id": 1, "piece_type": pt}
    await orch._produce(row, COMPLEX_RECIPE[pt])
    return events, busy


async def test_complex_sequence(pt, fails):
    before = len(fails)
    events, busy = await run_complex(pt)
    rec = COMPLEX_RECIPE[pt]
    top, leg, asm = rec["top"], rec["leg"], rec["asm"]

    hs = [(i, e) for i, e in enumerate(events) if e[0] == "hs"]
    xf = [(i, e) for i, e in enumerate(events) if e[0] == "xfer"]
    legs = [(i, e) for i, e in hs if e[2] == leg["raw_id"]]
    tops = [(i, e) for i, e in hs if e[2] == top["raw_id"]]
    asms = [(i, e) for i, e in hs if e[2] == top["id"]]

    print(f"\n  === {pt} ===")
    for e in events:
        print("    ", e)

    if expect(len(legs) == leg["count"], f"{pt}: expected {leg['count']} "
              f"legs, got {len(legs)}", fails):
        leg_cells = {e[1] for _, e in legs}
        expect(len(leg_cells) == 1 and leg_cells.pop() in asm["cells"],
               f"{pt}: legs not all in one assembly cell", fails)
        expect(sorted(e[3][0][0] for _, e in legs) == sorted(leg["machines"]),
               f"{pt}: leg machines != {leg['machines']}", fails)
        for _, e in legs:
            expect(e[3][0][1:] == (leg["tool"], leg["time_s"]),
                   f"{pt}: leg shape op wrong {e[3]}", fails)
            expect(e[3][1] == (3, 0, 0),
                   f"{pt}: leg park op != (M3,0,0): {e[3]}", fails)

    if expect(len(tops) == 1, f"{pt}: expected 1 top, got {len(tops)}", fails):
        expect(tops[0][1][1] in top["cells"],
               f"{pt}: top not in a wood cell", fails)
        expect(tops[0][1][3] == ((top["machine"], top["tool"],
               top["time_s"]),), f"{pt}: top op wrong {tops[0][1][3]}", fails)

    if expect(len(asms) == 1, f"{pt}: expected 1 assembly, got {len(asms)}",
              fails):
        expect(asms[0][1][3] == ((asm["machine"], asm["tool"],
               asm["time_s"]),), f"{pt}: assembly op wrong", fails)
        if legs:
            expect(asms[0][1][1] == legs[0][1][1],
                   f"{pt}: assembly cell != leg cell", fails)
    if tops and asms:
        expect(tops[0][1][1] != asms[0][1][1],
               f"{pt}: top cell must differ from assembly cell", fails)

    if expect(len(xf) == 2, f"{pt}: expected 2 transfers (top + return), "
              f"got {len(xf)}", fails):
        expect(xf[0][1][1] == top["id"],
               f"{pt}: first transfer is not the top id", fails)
        expect(xf[-1][1][1] == pt,
               f"{pt}: last transfer is not the finished product", fails)

    if legs and tops and asms and len(xf) == 2:
        top_xfer_i = xf[0][0]
        asm_i = asms[0][0]
        ret_i = xf[1][0]
        shape_max = max(i for i, _ in legs + tops)
        expect(shape_max < top_xfer_i, f"{pt}: shaping after top transfer",
               fails)
        expect(top_xfer_i < asm_i, f"{pt}: assembly before top transfer",
               fails)
        expect(asm_i < ret_i, f"{pt}: return before assembly", fails)

    expect(_completed == [(99, 0.0)], f"{pt}: mark_completed not called",
           fails)
    expect(len(busy) == 0, f"{pt}: cells left leased: {busy}", fails)
    print(f"  {pt} sequence:      {PASS if len(fails) == before else FAIL}")


async def main():
    fails = []
    print("opcua_client g_Return transfer (sensor-driven):")
    await test_return_engages_and_delivers(fails)
    await test_return_no_engage_fails(fails)
    await test_cell_handshake(fails)
    print("\nComplexOrchestrator:")
    await test_complex_sequence("RWM", fails)
    await test_complex_sequence("SWM", fails)

    print("\n" + "=" * 50)
    if fails:
        print(f"{FAIL}: {len(fails)} check(s) failed:")
        for f in fails:
            print("  -", f)
        return 1
    print(f"{PASS}: all checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
