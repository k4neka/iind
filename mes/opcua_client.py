"""OPC-UA client wrapping all PLC reads and writes."""
import asyncio
import datetime
from asyncua import Client, ua

from config import (OPCUA_URL_PRIMARY, OPCUA_URL_FALLBACK,
                    OPCUA_NS, OPCUA_PREFIX, LOADER_BATCH_SIZE)


# The MES no longer drives the Transfer_Cell corridor (v3 Bug 6): finished
# goods stay in W2 and the complex-piece top transfer is handled PLC-side by
# the Router. The corridor handshake helpers were removed accordingly.


class PLCClient:
    def __init__(self, on_reconnect=None):
        self.client: Client | None = None
        # Cached working VariantType for the CODESYS TIME field OpTime.
        self._optime_variant = None
        # Called after a *re*connect (not the first connect) so callers can
        # discard PLC-derived state — e.g. ToolStateTracker.reset(), since a
        # PLC restart returns every machine to its startup tool.
        self._on_reconnect = on_reconnect
        self._connected_before = False

    async def connect(self) -> bool:
        for url in (OPCUA_URL_PRIMARY, OPCUA_URL_FALLBACK):
            try:
                print(f"[opcua] connecting to {url}...")
                c = Client(url=url)
                await c.connect()
                self.client = c
                print(f"[opcua] connected to {url}")
                if self._connected_before and self._on_reconnect:
                    try:
                        self._on_reconnect()
                        print("[opcua] reconnect: tool state reset")
                    except Exception as e:
                        print(f"[opcua] on_reconnect hook failed: {e}")
                self._connected_before = True
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
    async def read_w1_counts(self) -> dict:
        """Read the PLC's authoritative raw-material counters in W1.

        GVL exposes `W1Count : ARRAY[1..2] OF UINT` where index 1 = Wood and
        index 2 = Metal (the WarehouseOut Receive_Workpiece_Data action
        decrements W1Count[InitPiece] as each raw piece leaves W1). This is
        now the single source of truth for how much Wood/Metal is physically
        in W1 — the MES no longer has to guess from dispatch bookkeeping.
        Returns {'Wood': n, 'Metal': m}; on any read error returns None so
        the caller keeps its previous estimate instead of zeroing it.
        """
        try:
            wood  = int(await self.read("W1Count[1]"))
            metal = int(await self.read("W1Count[2]"))
            return {"Wood": wood, "Metal": metal}
        except Exception as e:
            print(f"[opcua] read_w1_counts failed: {e}")
            return None

    async def read_w1_count(self) -> int:
        c = await self.read_w1_counts()
        return (c["Wood"] + c["Metal"]) if c else 0

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

        # 2. Write the route (InitPiece, ops, PieceID, ParentID).
        await self._write_workpiece_struct(base, init_piece, operations,
                                           piece_id=piece_id,
                                           parent_id=parent_id)

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
                                      operations: list[dict],
                                      piece_id: int = 0, parent_id: int = 0):
        """Write a full Workpiece_T at `base` (e.g.
        'Cell_3_Top_Order.Workpiece'): InitPiece, Next/Last_Operation,
        every Operation, and PieceID/ParentID. Each op field defaults to 0
        if absent. Does NOT touch recv_cmd.

        PieceID/ParentID drive the PLC completion buffer (g_Done): the M3
        Inc_Next_Oper action sets PieceID := ParentID, and Store_Piece_Ack
        writes a g_Done entry once a piece's ops are exhausted at a cell Win
        **and ParentID > 0**. Simple pieces now pass a non-zero ParentID
        (their pending_pieces.id) so they complete through g_Done exactly
        like complex pieces; leave both 0 for transport-only workpieces.

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
        await _w(f"{base}.PieceID", int(piece_id))
        await _w(f"{base}.ParentID", int(parent_id))
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
                              operations: list[dict],
                              piece_id: int = 0, parent_id: int = 0):
        """Populate Cell_X_Top_Order.Workpiece and raise recv_cmd.

        Caller is responsible for closing the handshake (lowering recv_cmd
        once the cell drops free_cmd, then waiting for it to rise again).
        """
        await self._write_workpiece_struct(
            f"Cell_{cell}_Top_Order.Workpiece", init_piece, operations,
            piece_id=piece_id, parent_id=parent_id)
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
                                       piece_id: int = 0, parent_id: int = 0,
                                       timeout: float = 60.0) -> bool:
        """Full single-piece handshake into a cell, mirroring the
        Order_generator step pattern: wait the cell head free -> write the
        workpiece + recv_cmd -> wait the cell accepts it (free_cmd drops)
        -> lower recv_cmd -> wait the head is ready again. Returns True on
        success.

        `parent_id` > 0 routes the piece's completion through the g_Done
        buffer (used for simple final products; legs pass a unique
        `piece_id` with the product's id as `parent_id`)."""
        if not await self._wait_until(lambda: self.cell_free(cell), timeout):
            print(f"[opcua] handshake Cell_{cell}: never free")
            return False

        await self.write_workpiece(cell=cell, init_piece=init_piece,
                                   operations=operations,
                                   piece_id=piece_id, parent_id=parent_id)

        if not await self._wait_until(lambda: self.cell_busy(cell), timeout):
            print(f"[opcua] handshake Cell_{cell}: free_cmd never dropped")
            await self.clear_recv_cmd(cell)
            return False

        await self.clear_recv_cmd(cell)

        # Head ready again -> ready for the next piece. Tolerate a timeout
        # here: the next handshake re-checks free at its start anyway.
        await self._wait_until(lambda: self.cell_free(cell), timeout)
        return True

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

    # ---- Unloading docks (PDF §2.4 / §4.1) ---------------------------------
    # The PLC exposes 5 Unloading_Dock FBs gated PLC-side by the addressed
    # dock id, so the MES drives a single scalar request slot:
    #   fill:      g_Unloader_DockID / g_Unloader_PieceID / g_Unloader_Qty
    #              + rising edge on g_Unloader_Exec
    #   discharge: g_Unloader_DischargeDockID / g_Unloader_DischargeQty
    #              + rising edge on g_Unloader_DischargeExec
    # Occupancy is read back from g_Unloader_DockCount[1..5]. Each FB latches
    # its command on a rising edge, so every request is a clean FALSE->TRUE
    # pulse with the scalars written FIRST, and we serialise one request at a
    # time (the slot is shared by all 5 docks).

    async def read_dock_count(self, dock: int) -> int:
        """Occupancy of one dock (1..5) from g_Unloader_DockCount."""
        try:
            return int(await self.read(f"g_Unloader_DockCount[{dock}]"))
        except Exception as e:
            print(f"[opcua] read_dock_count({dock}) failed: {e}")
            return 0

    async def read_all_dock_counts(self) -> dict:
        """{dock: count} for docks 1..5."""
        out = {}
        for d in range(1, 6):
            out[d] = await self.read_dock_count(d)
        return out

    async def unload_fill_dock(self, dock: int, piece_id: int, qty: int,
                               timeout: float = 30.0) -> bool:
        """Pull `qty` pieces of type `piece_id` out of W2 onto dock `dock`.

        Rising-edge handshake matching the Unloading_Dock FB (RReq):
          1. clear g_Unloader_Exec (unambiguous edge);
          2. write DockID, PieceID, Qty;
          3. raise g_Unloader_Exec;
          4. wait until g_Unloader_DockCount[dock] rose by `qty`;
          5. drop g_Unloader_Exec.

        Returns True if the dock occupancy reached the expected target within
        `timeout`. The dock count is the ground truth (the FB only accepts the
        request when (current + qty) <= 6, so we also verify capacity here).
        """
        if qty <= 0:
            return True
        try:
            before = await self.read_dock_count(dock)
            if before + qty > 6:
                print(f"[opcua] unload_fill_dock: dock {dock} cannot hold "
                      f"{qty} more (has {before}/6)")
                return False

            await self.write("g_Unloader_Exec", False, ua.VariantType.Boolean)
            await self.write("g_Unloader_DockID",  int(dock),
                             ua.VariantType.Int16)
            await self.write("g_Unloader_PieceID", int(piece_id),
                             ua.VariantType.Int16)
            await self.write("g_Unloader_Qty",     int(qty),
                             ua.VariantType.Int16)
            await self.write("g_Unloader_Exec", True, ua.VariantType.Boolean)

            target = before + qty
            ok = await self._wait_until(
                lambda: self._dock_at_least(dock, target), timeout)

            await self.write("g_Unloader_Exec", False, ua.VariantType.Boolean)
            if ok:
                print(f"[opcua] dock {dock}: filled {qty}x piece {piece_id} "
                      f"(now {await self.read_dock_count(dock)}/6)")
            else:
                print(f"[opcua] dock {dock}: fill timeout "
                      f"(have {await self.read_dock_count(dock)}, "
                      f"wanted {target})")
            return ok
        except Exception as e:
            print(f"[opcua] unload_fill_dock({dock}) failed: {e}")
            try:
                await self.write("g_Unloader_Exec", False,
                                 ua.VariantType.Boolean)
            except Exception:
                pass
            return False

    async def unload_discharge_dock(self, dock: int, qty: int | None = None,
                                    timeout: float = 30.0) -> bool:
        """Discharge (drop to the floor) pieces from dock `dock` at end-of-day.

        If `qty` is None the whole dock is emptied (current occupancy). Rising
        edge on g_Unloader_DischargeExec after writing the scalars; we then
        wait for the dock count to fall to (before - qty).
        """
        try:
            before = await self.read_dock_count(dock)
            if before <= 0:
                return True
            q = before if qty is None else min(qty, before)

            await self.write("g_Unloader_DischargeExec", False,
                             ua.VariantType.Boolean)
            await self.write("g_Unloader_DischargeDockID", int(dock),
                             ua.VariantType.Int16)
            await self.write("g_Unloader_DischargeQty", int(q),
                             ua.VariantType.Int16)
            await self.write("g_Unloader_DischargeExec", True,
                             ua.VariantType.Boolean)

            target = before - q
            ok = await self._wait_until(
                lambda: self._dock_at_most(dock, target), timeout)

            await self.write("g_Unloader_DischargeExec", False,
                             ua.VariantType.Boolean)
            if ok:
                print(f"[opcua] dock {dock}: discharged {q} "
                      f"(now {await self.read_dock_count(dock)}/6)")
            else:
                print(f"[opcua] dock {dock}: discharge timeout "
                      f"(have {await self.read_dock_count(dock)}, "
                      f"wanted {target})")
            return ok
        except Exception as e:
            print(f"[opcua] unload_discharge_dock({dock}) failed: {e}")
            try:
                await self.write("g_Unloader_DischargeExec", False,
                                 ua.VariantType.Boolean)
            except Exception:
                pass
            return False

    async def _dock_at_least(self, dock: int, target: int) -> bool:
        return (await self.read_dock_count(dock)) >= target

    async def _dock_at_most(self, dock: int, target: int) -> bool:
        return (await self.read_dock_count(dock)) <= target

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