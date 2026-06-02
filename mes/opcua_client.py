"""OPC-UA client wrapping all PLC reads and writes."""
import asyncio
import datetime
from asyncua import Client, ua

from config import (OPCUA_URL_PRIMARY, OPCUA_URL_FALLBACK,
                    OPCUA_NS, OPCUA_PREFIX, REG_SIZE, LOADER_BATCH_SIZE)


# Transfer_Cell I/O offsets, from PLC_PRG:
#   Transfer: Transfer_Cell(20, 45, 14, ... 10, 24, 0)
#   -> Wout sensor In[20], Wout request register Reg[14]; Win sensor In[10].
# These reflect the *physical* corridor and are reliable to read (unlike
# g_W1_Count / g_W2_Count, which the current PLC project leaves at 0).
TRANSFER_WOUT_IN = 20    # piece present at the corridor entrance (W2 side)
TRANSFER_WOUT_REG = 14   # piece id the corridor Wout is requesting from W2
TRANSFER_WIN_IN = 10     # piece present at the corridor exit (W1 side)
# Seconds to let the piece finish travelling the 5 corridor conveyors into
# W1 once it has been pulled out of W2. Generous; the assembly step also
# self-synchronises (its cell Wout waits for the piece in W1).
CORRIDOR_SETTLE_S = 6.0


class PLCClient:
    def __init__(self):
        self.client: Client | None = None
        # Cached working VariantType for the CODESYS TIME field OpTime.
        self._optime_variant = None

    async def connect(self) -> bool:
        for url in (OPCUA_URL_PRIMARY, OPCUA_URL_FALLBACK):
            try:
                print(f"[opcua] connecting to {url}...")
                c = Client(url=url)
                await c.connect()
                self.client = c
                print(f"[opcua] connected to {url}")
                return True
            except Exception as e:
                print(f"[opcua] failed {url}: {e}")
        return False

    async def disconnect(self):
        if self.client:
            await self.client.disconnect()
            self.client = None

    # ---- low-level helpers --------------------------------------------------
    def _node(self, var: str):
        return self.client.get_node(f"ns={OPCUA_NS};s={OPCUA_PREFIX}{var}")

    async def read(self, var: str):
        return await self._node(var).read_value()

    async def write(self, var: str, value,
                    varianttype: ua.VariantType | None = None):
        n = self._node(var)
        if varianttype is None:
            await n.write_value(value)
        else:
            await n.write_value(ua.DataValue(ua.Variant(value, varianttype)))

    async def _wait_until(self, predicate, timeout: float,
                          step: float = 0.1) -> bool:
        """Poll an async, zero-arg `predicate` until it returns truthy or
        `timeout` seconds elapse. A transient read error counts as 'not
        yet'. Returns True iff the condition was observed."""
        for _ in range(max(1, int(timeout / step))):
            try:
                if await predicate():
                    return True
            except Exception:
                pass
            await asyncio.sleep(step)
        return False

    # ---- high-level reads ---------------------------------------------------
    async def read_reg(self) -> list[int]:
        return [await self.read(f"Reg[{i}]") for i in range(REG_SIZE)]

    async def read_w1_count(self) -> int:
        try:
            return int(await self.read("g_W1_Count"))
        except Exception as e:
            print(f"[opcua] read_w1_count failed: {e}")
            return 0

    async def read_w2_count(self) -> int:
        try:
            return int(await self.read("g_W2_Count"))
        except Exception as e:
            print(f"[opcua] read_w2_count failed: {e}")
            return 0

    async def read_in(self, i: int) -> bool:
        """Read one physical input sensor GVL.In[i]."""
        try:
            return bool(await self.read(f"In[{i}]"))
        except Exception as e:
            print(f"[opcua] read In[{i}] failed: {e}")
            return False

    async def read_reg_index(self, i: int) -> int:
        """Read one warehouse request register GVL.Reg[i]."""
        try:
            return int(await self.read(f"Reg[{i}]"))
        except Exception as e:
            print(f"[opcua] read Reg[{i}] failed: {e}")
            return 0

    async def cell_free(self, cell: int) -> bool:
        return bool(await self.read(f"Cell_{cell}_Top_Status.free_cmd"))

    async def cell_busy(self, cell: int) -> bool:
        return not await self.cell_free(cell)

    # ---- Form A: Router intake + completion buffer -------------------------
    async def router_send_route(self, init_piece: int, operations: list[dict],
                                piece_id: int, parent_id: int) -> bool:
        """Hand the PLC-side Router_FB a fully-routed workpiece, with a REAL
        handshake so routes are never lost when several are sent back-to-back.

        Protocol (matches the Router intake state machine):
          1. wait until g_Router_Order.recv_cmd is low (Router ready);
          2. write the full Workpiece_T (route + PieceID/ParentID);
          3. raise recv_cmd;
          4. wait until the Router LOWERS recv_cmd (it does so when it has
             taken the job into a slot) -> proves it was consumed.

        Without step 4 the MES would overwrite g_Router_Order with the next
        route before the Router latched the previous one, dropping routes
        (the symptom: legs missing, tops duplicated)."""
        base = "g_Router_Order.Workpiece"

        # 1. Router must be ready (previous route already consumed).
        for _ in range(400):                       # ~20s ceiling
            try:
                if not await self.read("g_Router_Order.recv_cmd"):
                    break
            except Exception:
                pass
            await asyncio.sleep(0.05)
        else:
            print("[opcua] WARN: Router intake busy; sending route anyway")

        # 2. Write the route.
        await self._write_workpiece_struct(base, init_piece, operations)
        for field, val in (("PieceID", piece_id), ("ParentID", parent_id)):
            try:
                await self.write(f"{base}.{field}", int(val),
                                 ua.VariantType.Int16)
            except Exception as e:
                print(f"[opcua] WRITE FAILED at {base}.{field} = {val}: "
                      f"{type(e).__name__}: {e}  -> check this struct field "
                      f"exists and OPC-UA symbols were regenerated")
                raise

        # 3. Raise recv_cmd: the route is offered.
        await self.write("g_Router_Order.recv_cmd", True,
                         ua.VariantType.Boolean)

        # 4. Wait for the Router to CONSUME it (it lowers recv_cmd on take).
        for _ in range(400):                       # ~20s ceiling
            await asyncio.sleep(0.05)
            try:
                if not await self.read("g_Router_Order.recv_cmd"):
                    return True
            except Exception:
                pass
        print("[opcua] WARN: Router did not consume the route within timeout; "
              "it may have been dropped")
        # Drop it ourselves so a stuck recv_cmd doesn't wedge the next send.
        await self.write("g_Router_Order.recv_cmd", False,
                         ua.VariantType.Boolean)
        return False

    async def read_done_buffer(self, size: int = 16) -> list[dict]:
        """Read the completion buffer. Returns one dict per NON-EMPTY slot:
        {slot, piece_id, parent_id, type}. Empty slots (piece_id==0) are
        skipped. The MES is responsible for clearing each slot it processes
        via clear_done_slot()."""
        out = []
        for k in range(size):
            try:
                pid = int(await self.read(f"g_Done_PieceID[{k}]"))
            except Exception:
                continue
            if pid != 0:
                out.append({
                    "slot": k,
                    "piece_id": pid,
                    "parent_id": int(await self.read(f"g_Done_Parent[{k}]")),
                    "type": int(await self.read(f"g_Done_Type[{k}]")),
                })
        return out

    async def clear_done_slot(self, slot: int):
        """Clear a completion slot after the MES has processed it. Setting
        PieceID to 0 marks the slot empty; the PLC may then reuse it."""
        await self.write(f"g_Done_PieceID[{slot}]", 0, ua.VariantType.Int16)

    # ---- cell handshake -----------------------------------------------------
    async def _write_workpiece_struct(self, base: str, init_piece: int,
                                      operations: list[dict]):
        """Write a full Workpiece_T at `base` (e.g.
        'Cell_3_Top_Order.Workpiece'): InitPiece, Next/Last_Operation and
        every Operation. Each op field defaults to 0 if absent. Does NOT
        touch recv_cmd.

        Each write is individually guarded so a failure names the exact node
        that rebented, instead of a generic 'Failed to send request'."""
        async def _w(path, val, vt=ua.VariantType.Int16):
            try:
                await self.write(path, val, vt)
            except Exception as e:
                print(f"[opcua] WRITE FAILED at {path} = {val}: "
                      f"{type(e).__name__}: {e}")
                raise

        await _w(f"{base}.InitPiece", int(init_piece))
        await _w(f"{base}.Next_Operation", 0)
        await _w(f"{base}.Last_Operation", max(0, len(operations) - 1))
        for i, op in enumerate(operations[:11]):
            ob = f"{base}.Operations[{i}]"
            await _w(f"{ob}.Cell",    int(op.get("cell", 0)))
            await _w(f"{ob}.Machine", int(op.get("machine", 0)))
            await _w(f"{ob}.Tool",    int(op.get("tool", 0)))
            # PieceArg is non-fatal (may be missing if symbols not regenerated).
            pa = int(op.get("piece_arg", 0))
            try:
                await self.write(f"{ob}.PieceArg", pa, ua.VariantType.Int16)
            except Exception as e:
                if pa != 0:
                    print(f"[opcua] WARN: could not write {ob}.PieceArg={pa} "
                          f"({e}); transfer routing for this op will fail.")
            try:
                await self._write_optime(f"{ob}.OpTime", op.get("op_time_s", 0))
            except Exception as e:
                print(f"[opcua] WRITE FAILED at {ob}.OpTime = "
                      f"{op.get('op_time_s', 0)}s: {type(e).__name__}: {e}")
                raise

    async def write_workpiece(self, cell: int, init_piece: int,
                              operations: list[dict]):
        """Populate Cell_X_Top_Order.Workpiece and raise recv_cmd.

        Caller is responsible for closing the handshake (lowering recv_cmd
        once the cell drops free_cmd, then waiting for it to rise again).
        """
        await self._write_workpiece_struct(
            f"Cell_{cell}_Top_Order.Workpiece", init_piece, operations)
        await self.write(f"Cell_{cell}_Top_Order.recv_cmd",
                         True, ua.VariantType.Boolean)

    async def clear_recv_cmd(self, cell: int):
        # Lower recv_cmd after the cell accepted the workpiece, matching
        # the Order_generator handshake pattern.
        await self.write(f"Cell_{cell}_Top_Order.recv_cmd",
                         False, ua.VariantType.Boolean)

    # ---- blocking handshakes (used by orchestrators) -----------------------
    async def send_workpiece_handshake(self, cell: int, init_piece: int,
                                       operations: list[dict],
                                       timeout: float = 60.0) -> bool:
        """Full single-piece handshake into a cell, mirroring the
        Order_generator step pattern: wait the cell head free -> write the
        workpiece + recv_cmd -> wait the cell accepts it (free_cmd drops)
        -> lower recv_cmd -> wait the head is ready again. Returns True on
        success."""
        if not await self._wait_until(lambda: self.cell_free(cell), timeout):
            print(f"[opcua] handshake Cell_{cell}: never free")
            return False

        await self.write_workpiece(cell=cell, init_piece=init_piece,
                                   operations=operations)

        if not await self._wait_until(lambda: self.cell_busy(cell), timeout):
            print(f"[opcua] handshake Cell_{cell}: free_cmd never dropped")
            await self.clear_recv_cmd(cell)
            return False

        await self.clear_recv_cmd(cell)

        # Head ready again -> ready for the next piece. Tolerate a timeout
        # here: the next handshake re-checks free at its start anyway.
        await self._wait_until(lambda: self.cell_free(cell), timeout)
        return True

    async def _transfer_snapshot(self) -> dict:
        """Read every signal that reflects the corridor, for detection and
        diagnostics. g_W1/g_W2 are included but are unreliable in the
        current PLC project (they stay 0); In[]/Reg[] are authoritative."""
        return {
            "w1": await self.read_w1_count(),
            "w2": await self.read_w2_count(),
            "wout_in": await self.read_in(TRANSFER_WOUT_IN),
            "win_in": await self.read_in(TRANSFER_WIN_IN),
            "wout_reg": await self.read_reg_index(TRANSFER_WOUT_REG),
        }

    # ---- Transfer_Order helpers (corridor handshake, mirrors a cell) -------
    async def _transfer_free(self) -> bool:
        return bool(await self.read("Transfer_Status.free_cmd"))

    async def _transfer_cmd(self) -> bool:
        return bool(await self.read("Transfer_Cmd"))

    async def _transfer_done(self) -> bool:
        return bool(await self.read("Transfer_Done"))

    async def transfer_piece_blocking(self, piece_id: int,
                                      ready_timeout: float = 180.0,
                                      deliver_timeout: float = 60.0) -> bool:
        """Return one piece from W2 to W1 through the Transfer_Cell.

        This reproduces, over OPC-UA, the exact corridor handshake the
        Order_generator used (steps 0/3/30/31), now that we know the gate
        signals are produced *natively* by the PLC inside
        WarehouseIn_Conveyor.Warehouse_Store_Piece:

            * when ANY piece reaches W2 (a Win with Qoff <> 24), and
              Transfer_Activate is TRUE, the PLC raises Transfer_Cmd.
            * when a piece reaches W1 through the corridor (the corridor's
              Win, Qoff = 24), the PLC raises Transfer_Done.

        So the MES must (a) arm the corridor by setting Transfer_Activate,
        (b) wait for Transfer_Cmd (proof the piece is physically in W2 — no
        premature fire), (c) issue the Transfer_Order workpiece + recv_cmd
        and clear Transfer_Cmd, (d) lower recv_cmd, (e) wait Transfer_Done
        (piece delivered to W1), (f) disarm Transfer_Activate. This is the
        decisive fix: the previous version never set Transfer_Activate, so
        the PLC never raised Transfer_Cmd and the piece froze in W2.

        `piece_id` is the piece TYPE requested out of W2 (InitPiece: RtopW=3
        for a complex top, or the final-product id for a finished-goods
        return). The corridor's WarehouseOut writes this into Reg[14] to
        pull the matching piece out of W2.
        """
        s0 = await self._transfer_snapshot()
        print(f"[opcua] transfer: requesting piece {piece_id} W2->W1 "
              f"(In[20]={s0['wout_in']} Reg[14]={s0['wout_reg']} "
              f"In[10]={s0['win_in']})")

        # 0. Arm the corridor and clear stale latches. With Transfer_Activate
        #    TRUE, the W2 Win will raise Transfer_Cmd when the piece arrives.
        await self.write("Transfer_Done", False, ua.VariantType.Boolean)
        await self.write("Transfer_Cmd", False, ua.VariantType.Boolean)
        await self.write("Transfer_Activate", True, ua.VariantType.Boolean)

        try:
            # 1. Corridor head must be free, and the piece must have reached
            #    W2 (Transfer_Cmd). This is the gate that was missing.
            async def ready():
                return await self._transfer_cmd() and await self._transfer_free()

            if not await self._wait_until(ready, ready_timeout):
                snap = await self._transfer_snapshot()
                cmd = await self._transfer_cmd()
                print(f"[opcua] transfer {piece_id}: not ready within "
                      f"{ready_timeout:.0f}s (Transfer_Cmd={cmd}; piece may "
                      f"not have reached W2). signals={snap}")
                return False

            # 2. Issue the transport-only workpiece (InitPiece = piece_id, no
            #    operations) and raise recv_cmd; clear Transfer_Cmd, exactly
            #    like Order_generator step 30.
            await self._write_workpiece_struct(
                "Transfer_Order.Workpiece", piece_id, operations=[])
            await self.write("Transfer_Order.recv_cmd", True,
                             ua.VariantType.Boolean)
            await self.write("Transfer_Cmd", False, ua.VariantType.Boolean)

            # 3. Lower recv_cmd (step 31) and wait for delivery to W1, which
            #    the PLC signals by raising Transfer_Done at the corridor Win.
            await self.write("Transfer_Order.recv_cmd", False,
                             ua.VariantType.Boolean)

            if not await self._wait_until(self._transfer_done,
                                          deliver_timeout):
                snap = await self._transfer_snapshot()
                print(f"[opcua] transfer {piece_id}: Transfer_Done never "
                      f"raised within {deliver_timeout:.0f}s. signals={snap}")
                return False

            # Let the piece settle into W1 before the next transfer engages.
            await asyncio.sleep(CORRIDOR_SETTLE_S)
            print(f"[opcua] transfer: piece {piece_id} delivered to W1")
            return True
        finally:
            # Disarm and clear latches so the next transfer re-arms cleanly,
            # mirroring Order_generator step 31 (Transfer_Activate := FALSE).
            await self.write("Transfer_Order.recv_cmd", False,
                             ua.VariantType.Boolean)
            await self.write("Transfer_Done", False, ua.VariantType.Boolean)
            await self.write("Transfer_Cmd", False, ua.VariantType.Boolean)
            await self.write("Transfer_Activate", False, ua.VariantType.Boolean)

    # ---- OpTime VariantType probing ----------------------------------------
    async def _write_optime(self, node_path: str, op_time_s: float):
        node = self._node(node_path)
        ms = int(op_time_s * 1000)

        if self._optime_variant is not None:
            return await self._write_optime_with(node, ms,
                                                 self._optime_variant)

        candidates = [
            ua.VariantType.Int64,
            ua.VariantType.Int32,
            ua.VariantType.UInt64,
            ua.VariantType.UInt32,
            ua.VariantType.Double,
            ua.VariantType.Float,
            "timedelta",
        ]
        try:
            current = await node.read_value()
            if isinstance(current, datetime.timedelta):
                candidates = ["timedelta"] + [c for c in candidates
                                              if c != "timedelta"]
            elif isinstance(current, float):
                candidates = [ua.VariantType.Double, ua.VariantType.Float] + \
                             [c for c in candidates
                              if c not in (ua.VariantType.Double,
                                           ua.VariantType.Float)]
        except Exception:
            pass

        last_err = None
        for vt in candidates:
            try:
                await self._write_optime_with(node, ms, vt)
                self._optime_variant = vt
                name = vt if isinstance(vt, str) else vt.name
                print(f"[opcua] OpTime VariantType detected: {name}")
                return
            except Exception as e:
                last_err = e
        print(f"[opcua] OpTime: all write attempts failed: {last_err}")
        raise last_err

    async def _write_optime_with(self, node, ms: int, vt):
        if vt == "timedelta":
            await node.write_value(datetime.timedelta(milliseconds=ms))
        elif vt in (ua.VariantType.Double, ua.VariantType.Float):
            await node.write_value(ua.DataValue(ua.Variant(float(ms), vt)))
        else:
            await node.write_value(ua.DataValue(ua.Variant(int(ms), vt)))

    # ---- Loader -------------------------------------------------------------
    async def trigger_loader_batch(self, wood_qty: int, metal_qty: int):
        """Fire one batch (<= LOADER_BATCH_SIZE pieces) onto the Loader."""
        if wood_qty == 0 and metal_qty == 0:
            return
        try:
            # Clear Exec first so the rising edge is unambiguous.
            await self.write("g_Loader_Exec", False, ua.VariantType.Boolean)

            # Wait for IDLE before writing new targets.
            for _ in range(50):
                if (await self.read_loader_status()) == 0:
                    break
                await asyncio.sleep(0.1)
            else:
                print("[opcua] trigger_loader_batch: PLC never reached IDLE")
                return

            await self.write("g_Loader_Wood_Qty",  int(wood_qty),
                             ua.VariantType.Int16)
            await self.write("g_Loader_Metal_Qty", int(metal_qty),
                             ua.VariantType.Int16)
            await self.write("g_Loader_Exec", True, ua.VariantType.Boolean)
            print(f"[opcua] trigger_loader_batch wood={wood_qty} "
                  f"metal={metal_qty}")
        except Exception as e:
            print(f"[opcua] trigger_loader_batch failed: {e}")

    async def trigger_loader(self, wood_qty: int, metal_qty: int):
        # Backwards-compat alias: single shot, no batching.
        await self.trigger_loader_batch(wood_qty, metal_qty)

    async def trigger_loader_batched(self, wood_qty: int, metal_qty: int):
        """Split a large request into batches of LOADER_BATCH_SIZE pieces.
        Wood is delivered first, then Metal. Waits for DONE between batches.
        """
        remaining_w = max(0, wood_qty)
        remaining_m = max(0, metal_qty)
        batch_num = 0

        while remaining_w > 0 or remaining_m > 0:
            batch_num += 1
            take_w = min(remaining_w, LOADER_BATCH_SIZE)
            take_m = min(remaining_m, LOADER_BATCH_SIZE - take_w)
            print(f"[opcua] batched-load: batch #{batch_num} "
                  f"wood={take_w} metal={take_m} "
                  f"(remaining wood={remaining_w - take_w}, "
                  f"metal={remaining_m - take_m})")

            await self.trigger_loader_batch(take_w, take_m)
            remaining_w -= take_w
            remaining_m -= take_m

            # Wait DONE (up to 120 s per batch).
            done = False
            for _ in range(1200):
                status = await self.read_loader_status()
                if status == 2:
                    done = True
                    break
                if status == 3:
                    print(f"[opcua] batched-load: ERROR on batch "
                          f"#{batch_num}")
                    return
                await asyncio.sleep(0.1)
            if not done:
                print(f"[opcua] batched-load: batch #{batch_num} timeout")
                return

            # Clear Exec so next batch produces a rising edge.
            await self.clear_loader_exec()
            for _ in range(50):
                if (await self.read_loader_status()) == 0:
                    break
                await asyncio.sleep(0.1)

        print(f"[opcua] batched-load: complete "
              f"(total wood={wood_qty} metal={metal_qty})")

    async def read_loader_status(self) -> int:
        try:
            return int(await self.read("g_Loader_Status"))
        except Exception as e:
            print(f"[opcua] read_loader_status failed: {e}")
            return 0

    async def clear_loader_exec(self):
        try:
            await self.write("g_Loader_Exec", False, ua.VariantType.Boolean)
        except Exception as e:
            print(f"[opcua] clear_loader_exec failed: {e}")

    # ---- Unloader -----------------------------------------------------------
    async def trigger_unloader(self, piece_type: str, qty: int):
        try:
            await self.write("g_Unloader_PieceType", str(piece_type),
                             ua.VariantType.String)
            await self.write("g_Unloader_Qty", int(qty),
                             ua.VariantType.Int16)
            await self.write("g_Unloader_Exec", True, ua.VariantType.Boolean)
            print(f"[opcua] trigger_unloader type={piece_type} qty={qty}")
        except Exception as e:
            print(f"[opcua] trigger_unloader failed: {e}")

    # The W2->W1 return is driven as a full blocking handshake on
    # Transfer_Order / Transfer_Status by transfer_piece_blocking() above.
    # (g_Return_PieceID / g_Return_Exec are unused: no PLC program consumes
    # them, so they never moved the corridor.)