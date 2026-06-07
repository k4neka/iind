"""Unloading-dock manager (PDF §2.4 / §4.1).

Whole-order delivery model
==========================
A client order LINE is delivered as one unit: all N pieces of the line are
accumulated on its reserved dock(s), and the line is DISCHARGED (the real
dispatch to the client -- pieces slide off to the floor) only when its full N
are present. The MES owns dock allocation entirely; the PLC stays type-only.

Pieces are NOT reserved per client. A finished piece of a given type is pulled
onto whichever line needs that type and is MOST URGENT (earliest due-date,
ties broken by oldest line). So an urgent late order can borrow finished
pieces another long-deadline order's production created -- every line still
keeps its own `need`, so the borrowed-from line is simply topped up by pieces
produced later. (The ERP tracks produced-vs-quantity and reschedules the
shortfall, including buying the extra raw material.)

State
-----
line book   : per order_line_id -> {client, order, piece_type, need, ddate,
                                     target, docks:set, on_docks}
              `need`     = pieces of this line not yet placed on a dock
              `on_docks` = pieces of this line currently sitting on its docks
              `target`   = line total quantity (whole-order threshold)
W2 ledger   : {piece_type: qty} finished goods available in W2, credited by
              completion_loop, debited when a piece is pulled onto a dock.
dock owner  : {dock: order_line_id} -- a dock holds exactly one line's order,
              so it can be discharged whole. Capacity DOCK_CAPACITY (6).

A single asyncio.Lock serialises the OPC-UA request slot shared by all docks.
"""
import asyncio

from config import NUM_DOCKS, DOCK_CAPACITY, PIECE_ID
from database import (unloader_save_books, unloader_load_books,
                      unloader_record_unloaded)


class UnloaderManager:
    def __init__(self, plc, publish_status=None):
        self.plc = plc
        self.publish_status = publish_status
        self._req_lock = asyncio.Lock()      # shared PLC request slot
        self._state_lock = asyncio.Lock()    # guards the books below

        # order_line_id -> line record
        self._lines = {}
        # piece_type -> available finished units in W2
        self._w2_stock = {}
        # dock -> order_line_id that owns it (None = free)
        self._dock_owner = {d: None for d in range(1, NUM_DOCKS + 1)}
        # dock -> pieces currently on it (local mirror of g_Unloader_DockCount)
        self._dock_count = {d: 0 for d in range(1, NUM_DOCKS + 1)}
        self._seeded = False
        # The books (lines / W2 / dock ownership) are loaded from the DB once on
        # first run so a mid-day MES restart resumes them (TASK 4).
        self._loaded = False
        # §4.3 statistic: pieces discharged per dock (lifetime).
        self._discharged = {d: 0 for d in range(1, NUM_DOCKS + 1)}

    # ---- persistence (TASK 4) ------------------------------------------

    def _persist(self):
        """Write-through the three small books. Synchronous (like every other
        DB call in the codebase); the books are tiny so rewriting them whole on
        each mutation is cheap and keeps the DB an exact mirror for restart +
        the dashboard."""
        try:
            unloader_save_books(self._lines, self._w2_stock,
                                self._dock_owner, self._dock_count)
        except Exception as e:
            print(f"[unload] persist failed: {e}")

    # ---- visibility -----------------------------------------------------

    def w2_stock_total(self):
        return sum(self._w2_stock.values())

    def status_snapshot(self):
        """For a UI / debugging: each line and the dock(s) assigned to it."""
        return {lid: {"client": L["client"], "order": L["order"],
                      "piece_type": L["piece_type"], "target": L["target"],
                      "need": L["need"], "on_docks": L["on_docks"],
                      "docks": sorted(L["docks"])}
                for lid, L in self._lines.items()}

    # ---- completion credit (called by completion_loop) -----------------

    def credit_w2(self, piece_type, qty=1):
        if not piece_type:
            return
        self._w2_stock[piece_type] = self._w2_stock.get(piece_type, 0) + qty
        print(f"[unload] W2 +{qty} {piece_type} "
              f"(available {self._w2_stock[piece_type]})")
        # Persist the W2 ledger so a restart resumes the same finished-goods
        # credit (TASK 4). Called from completion_loop on the asyncio thread.
        self._persist()

    # ---- delivery-order intake -----------------------------------------

    async def handle_delivery_order(self, items):
        """Register/grow a line's whole-order demand from an ERP delivery item.

        Each item carries the FULL line context (client, line_quantity, ddate).
        The per-day delivery slices for the same order_line_id are coalesced:
        we set the line `target` to the line total and bump `need` so the line
        eventually accumulates its whole quantity on its dock(s).
        """
        async with self._state_lock:
            for it in items:
                lid = it.get("order_line_id")
                ptype = it.get("piece_type")
                if lid is None or not ptype or ptype not in PIECE_ID:
                    print(f"[unload] bad delivery item {it}; skipping")
                    continue
                qty = int(it.get("quantity", 0))
                target = int(it.get("line_quantity") or 0) or None
                ddate = it.get("ddate")

                L = self._lines.get(lid)
                if L is None:
                    L = {
                        "client": it.get("client_name"),
                        "order":  it.get("client_order_id"),
                        "piece_type": ptype,
                        "ddate":  (int(ddate) if ddate is not None else 10**9),
                        "target": target if target is not None else qty,
                        "need":   0,
                        "on_docks": 0,
                        "docks":  set(),
                    }
                    self._lines[lid] = L
                else:
                    if target is not None:
                        L["target"] = max(L["target"], target)
                    if ddate is not None:
                        L["ddate"] = int(ddate)

                # `need` is what still has to reach a dock to hit the target.
                still = max(0, L["target"] - L["on_docks"])
                L["need"] = still
                print(f"[unload] line {lid} (client={L['client']} "
                      f"order={L['order']} {ptype}x{L['target']} "
                      f"ddate={L['ddate']}): need {L['need']} on docks")
            self._persist()

    # ---- background loop -----------------------------------------------

    async def run_loop(self, interval=1.0):
        while True:
            try:
                await self._drain_pending()
                await self._discharge_complete_lines()
            except Exception as e:
                print(f"[unload] loop error: {e}")
            await asyncio.sleep(interval)

    # ---- placing pieces onto docks (most-urgent line first) ------------

    async def _drain_pending(self):
        if not self._loaded:
            await self._ensure_loaded()

        # Lines that still need pieces, most urgent first (ddate, then id).
        async with self._state_lock:
            hungry = [(lid, L) for lid, L in self._lines.items()
                      if L["need"] > 0]
            hungry.sort(key=lambda kv: (kv[1]["ddate"], kv[0]))

        for lid, L in hungry:
            ptype = L["piece_type"]
            async with self._state_lock:
                avail = self._w2_stock.get(ptype, 0)
                want = min(L["need"], avail)
            if want <= 0:
                continue

            # Reserve dock space for THIS line (its own docks only), then fill.
            placed_total = 0
            while placed_total < want:
                dock, room = await self._dock_for_line(lid, L)
                if dock is None:
                    print(f"[unload] line {lid}: no dock space available yet; "
                          f"retrying (owners {self._dock_owner})")
                    break
                n = min(room, want - placed_total)
                ok = await self._fill(dock, PIECE_ID[ptype], ptype, n, lid)
                if not ok:
                    print(f"[unload] line {lid}: fill {n}x {ptype} on dock "
                          f"{dock} failed; retry next tick")
                    break
                placed_total += n
                async with self._state_lock:
                    L["on_docks"] += n
                    L["need"] = max(0, L["target"] - L["on_docks"])
                    L["docks"].add(dock)
                    self._w2_stock[ptype] = max(
                        0, self._w2_stock.get(ptype, 0) - n)

            if placed_total > 0:
                self._persist()
                print(f"[unload] line {lid} (client={L['client']} "
                      f"order={L['order']}): {L['on_docks']}/{L['target']} on "
                      f"dock(s) {sorted(L['docks'])}, {L['need']} still needed")

    async def _dock_for_line(self, lid, L):
        """Pick a dock this line may use: one it already owns with room, else
        a free dock newly assigned to it. Returns (dock, free_room) or
        (None, 0). A dock holds exactly ONE line so it discharges whole."""
        # 1. a dock already owned by this line with spare room.
        for d in sorted(L["docks"]):
            room = DOCK_CAPACITY - self._dock_count[d]
            if room > 0:
                return d, room
        # 2. a free dock -> assign it to this line.
        for d in range(1, NUM_DOCKS + 1):
            if self._dock_owner[d] is None and self._dock_count[d] == 0:
                self._dock_owner[d] = lid
                return d, DOCK_CAPACITY
        return None, 0

    async def _fill(self, dock, pid, ptype, n, lid):
        async with self._req_lock:
            ok = await self.plc.unload_fill_dock(dock, pid, n)
            self._dock_count[dock] = await self.plc.read_dock_count(dock)
        if ok:
            print(f"[unload] placed {n}x {ptype} on dock {dock} for line {lid} "
                  f"(dock {self._dock_count[dock]}/{DOCK_CAPACITY})")
            if self.publish_status:
                self.publish_status({
                    "event":         "DOCK_LOADED",
                    "dock":          dock,
                    "order_line_id": lid,
                    "piece_type":    ptype,
                    "quantity":      n,
                })
        return ok

    # ---- whole-order discharge -----------------------------------------

    async def _discharge_complete_lines(self):
        """Discharge any line whose full target is sitting on its dock(s).
        This is the ACTUAL dispatch to the client -- a whole order at once."""
        async with self._state_lock:
            complete = [(lid, L) for lid, L in self._lines.items()
                        if L["target"] > 0 and L["on_docks"] >= L["target"]]
        for lid, L in complete:
            await self._discharge_line(lid, L, reason="order complete")

    async def _discharge_line(self, lid, L, reason):
        docks = sorted(L["docks"])
        print(f"[unload] DISPATCH line {lid} -> client={L['client']} "
              f"order={L['order']} {L['piece_type']}x{L['target']} "
              f"from dock(s) {docks} ({reason})")
        for dock in docks:
            have = self._dock_count[dock]
            if have <= 0:
                continue
            async with self._req_lock:
                ok = await self.plc.unload_discharge_dock(dock, qty=have)
                self._dock_count[dock] = await self.plc.read_dock_count(dock)
            if ok:
                self._discharged[dock] += have
                # Lifetime per-dock per-type tally (TASK 4 / Req 4.3).
                unloader_record_unloaded(dock, L["piece_type"], have)
                if self.publish_status:
                    self.publish_status({
                        "event":         "ORDER_DISPATCHED",
                        "dock":          dock,
                        "order_line_id": lid,
                        "client":        L["client"],
                        "order":         L["order"],
                        "piece_type":    L["piece_type"],
                        "quantity":      have,
                    })
        async with self._state_lock:
            for d in L["docks"]:
                if self._dock_owner.get(d) == lid:
                    self._dock_owner[d] = None
            self._lines.pop(lid, None)
            self._persist()

    # ---- end of day (backstop) -----------------------------------------

    async def discharge_all(self):
        """End-of-day backstop. Whole, complete lines are normally dispatched
        the moment they fill; here we flush anything still sitting on docks so
        a partially-built urgent order is still delivered (late) rather than
        held. Each owned dock is discharged and its line cleared."""
        if not self._loaded:
            await self._ensure_loaded()
        async with self._state_lock:
            lines = list(self._lines.items())
        flushed = False
        for lid, L in lines:
            if L["on_docks"] > 0:
                flushed = True
                await self._discharge_line(
                    lid, L, reason="end-of-day flush (partial)")
        # Any orphan dock occupancy with no line record -> clear it too.
        counts = await self.plc.read_all_dock_counts()
        for dock in range(1, NUM_DOCKS + 1):
            have = counts.get(dock, 0)
            if have > 0 and self._dock_owner[dock] is None:
                flushed = True
                async with self._req_lock:
                    ok = await self.plc.unload_discharge_dock(dock, qty=have)
                    self._dock_count[dock] = await self.plc.read_dock_count(dock)
                if ok:
                    self._discharged[dock] += have
                    print(f"[unload] end-of-day: cleared orphan dock {dock} "
                          f"({have} pieces)")
                    # Orphan docks have no line record, so the piece type is
                    # unknown -> only the dock count is reconciled, not the
                    # per-type tally.
        self._persist()
        if not flushed:
            print("[unload] end-of-day: nothing on docks to dispatch")

    # ---- helpers --------------------------------------------------------

    async def _ensure_loaded(self):
        """First-run init (TASK 4): restore the persisted books, then snap dock
        occupancy to the PLC (ground truth — pieces physically stay on docks
        across an MES restart). A dock the PLC shows empty cannot still be owned,
        so a stale owner with no pieces is cleared."""
        if self._loaded:
            return
        try:
            lines, w2, owners = unloader_load_books()
            if lines:
                self._lines = lines
            if w2:
                self._w2_stock = w2
            for d, owner in owners.items():
                if d in self._dock_owner:
                    self._dock_owner[d] = owner
            if lines or w2 or any(owners.values()):
                print(f"[unload] resumed {len(self._lines)} line(s), "
                      f"W2 ledger {self._w2_stock}, dock owners "
                      f"{ {d: o for d, o in self._dock_owner.items() if o} }")
        except Exception as e:
            print(f"[unload] load books failed (starting fresh): {e}")

        counts = await self.plc.read_all_dock_counts()
        for d in range(1, NUM_DOCKS + 1):
            self._dock_count[d] = counts.get(d, 0)
            if self._dock_count[d] == 0:
                self._dock_owner[d] = None      # no pieces -> ownership is stale
        self._loaded = True
        self._seeded = True
        self._persist()