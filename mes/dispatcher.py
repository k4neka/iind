"""Dispatcher: sends final products to free cells as sequential subparts.

Key improvements vs previous version:

* Persistent round-robin across capable cells, kept on the Dispatcher
  instance. Two separate single-piece orders for the same final
  product now land on different cells if both are free, instead of
  piling onto Cell_1.
* `_choose_free_cell` actively scans every capable cell on every tick
  and picks the first one that is both PLC-free and not already busy
  in this MES, *independently* of the optimiser's hint.
* Cells in `_busy_cells` are released only after every subpart of the
  chunk has been accepted by the cell head, preserving the original
  pipeline guarantee.

This module also hosts the ComplexOrchestrator (RWM/SWM), the sibling
strategy that builds mixed-material pieces across two cells + the Transfer
corridor. Both the Dispatcher (simple pieces) and the orchestrator share
the same `_busy_cells` lease registry so they never claim the same
physical cell at once.
"""
import asyncio
from datetime import datetime, timezone

from config import NUM_CELLS, COMPLEX_PIECES, COMPLEX_RECIPE
from database import (queued_pieces, mark_dispatched, mark_cell_started,
                      set_dispatched_ops)
from optimizer import optimise_batch
from transformations import ID_TO_PIECE
from piece_id import next_piece_id

# Machine slot used as the in-cell assembly buffer / assembly station for
# complex pieces.
M3 = 3


class Dispatcher:
    def __init__(self, plc, mqtt_bridge, busy_cells=None, busy_lock=None,
                 tool_tracker=None):
        self.plc = plc
        self.mqtt = mqtt_bridge
        self.tools = tool_tracker
        self._tick_count = 0
        self._last_log_tick = -100
        # Shared with the ComplexOrchestrator when injected, so the two
        # never dispatch into the same physical cell simultaneously.
        self._busy_cells: set[int] = (busy_cells if busy_cells is not None
                                      else set())
        self._busy_lock = busy_lock or asyncio.Lock()
        # Persistent round-robin offsets per capable-cell family.
        # Survives across optimise_batch() calls so two separate
        # orders for the same final product spread across cells.
        self._family_rr: dict[tuple, int] = {}

    # ---- helpers --------------------------------------------------------

    async def _cell_free_plc(self, cell: int) -> bool:
        try:
            return await self.plc.cell_free(cell)
        except Exception as e:
            print(f"[disp] cell_free({cell}) error: {e}")
            return False

    def _w1_local(self):
        return self.mqtt.w1_estimate()

    def _w1_total(self):
        e = self._w1_local()
        return e.get("Wood", 0) + e.get("Metal", 0)

    def _need_for(self, subparts):
        need = {"Wood": 0, "Metal": 0}
        for sp in subparts:
            raw_name = ID_TO_PIECE.get(sp["raw"])
            if raw_name in need:
                need[raw_name] += 1
        return need

    def _can_afford(self, subparts):
        need = self._need_for(subparts)
        est = self._w1_local()
        return all(est.get(k, 0) >= v for k, v in need.items())

    def _consume_w1(self, subparts):
        for sp in subparts:
            raw_name = ID_TO_PIECE.get(sp["raw"])
            if raw_name in ("Wood", "Metal"):
                self.mqtt.w1_consume(raw_name, 1)

    async def _choose_free_cell(self, capable, target_hint):
        """Return the first cell in `capable` that is both PLC-free and not
        held by another in-flight chunk. `target_hint` is tried first to
        honour the optimiser's preference."""
        ordered = []
        if target_hint in capable:
            ordered.append(target_hint)
        for c in capable:
            if c not in ordered:
                ordered.append(c)

        for c in ordered:
            if c in self._busy_cells:
                continue
            if await self._cell_free_plc(c):
                return c
        return None

    # ---- main tick ------------------------------------------------------

    async def tick(self):
        self._tick_count += 1
        pending = queued_pieces()
        if not pending:
            return

        verbose = (self._tick_count - self._last_log_tick) >= 20
        if verbose:
            self._last_log_tick = self._tick_count
            states = {}
            for c in range(1, NUM_CELLS + 1):
                fp = await self._cell_free_plc(c)
                states[c] = fp and (c not in self._busy_cells)
            est = self._w1_local()
            print(f"[disp] tick #{self._tick_count}: "
                  f"{len(pending)} queued, "
                  f"w1=wood:{est.get('Wood',0)}/"
                  f"metal:{est.get('Metal',0)}, "
                  f"free={states}")

        if self._w1_total() == 0:
            if verbose:
                print("[disp] W1 empty - waiting for material")
            return

        chunks, new_rr = optimise_batch(
            pending, rr_offset_by_family=self._family_rr,
            tool_state=(self.tools.all_cells_snapshot() if self.tools
                        else None),
        )
        # Persist round-robin so the NEXT tick (and the next replan
        # triggered by a new client order) keeps rotating cells.
        self._family_rr = new_rr

        if not chunks:
            return

        for chunk_rows, target_cell, capable, subparts in chunks:
            if not self._can_afford(subparts):
                if verbose:
                    est = self._w1_local()
                    need = self._need_for(subparts)
                    print(f"[disp] chunk of {len(chunk_rows)} "
                          f"({chunk_rows[0]['piece_type']}) waiting - "
                          f"need wood:{need['Wood']} "
                          f"metal:{need['Metal']} "
                          f"(have wood:{est.get('Wood',0)} "
                          f"metal:{est.get('Metal',0)})")
                continue

            async with self._busy_lock:
                cell = await self._choose_free_cell(capable, target_cell)
                if cell is None:
                    if verbose:
                        print(f"[disp] chunk of {len(chunk_rows)} "
                              f"({chunk_rows[0]['piece_type']}) "
                              f"waiting - no capable cell free "
                              f"(capable={capable})")
                    continue
                self._busy_cells.add(cell)

            # Rewrite ops to reflect the cell actually chosen.
            if cell != target_cell:
                for sp in subparts:
                    for op in sp["ops"]:
                        op["cell"] = cell

            self._consume_w1(subparts)
            # One product per chunk: one wire PieceID/ParentID from the shared
            # generator (INT16-safe, never the unbounded DB serial id). The DB
            # row stores it so completion_loop can map g_Done back to the row.
            wire = next_piece_id()
            row = chunk_rows[0]
            # Advance the tool tracker and capture per-op statistics records
            # NOW, at dispatch (TASK 1): apply_ops returns one record per real
            # op {cell, slot, tool, time_s, out, tool_change}. Persisting them
            # on the row means the machine work is committed to the stats tables
            # when the piece completes, even across an MES restart.
            op_records = []
            if self.tools:
                for sp in subparts:
                    op_records.extend(self.tools.apply_ops(cell, sp["ops"]))
            mark_dispatched(row["id"], cell, subparts[0]["raw"],
                            wire_piece_id=wire, dispatched_ops=op_records)
            print(f"[disp] {row['piece_type']} (db id {row['id']}, "
                  f"wire {wire}) -> Cell_{cell} as {len(subparts)} subparts "
                  f"(hint was Cell_{target_cell})")

            asyncio.create_task(
                self._run_chunk(chunk_rows, cell, subparts, wire)
            )

    # ---- subpart send loop ---------------------------------------------

    async def _run_chunk(self, chunk_rows, cell, subparts, wire):
        try:
            for idx, sp in enumerate(subparts, start=1):
                # PieceID/ParentID route completion through g_Done. The final
                # (assembled) subpart carries the product's wire id as both
                # PieceID and ParentID, so Store_Piece_Ack fires a g_Done entry
                # at the cell Win (completion_loop maps wire -> row, marks
                # COMPLETED). Parked legs carry their own wire PieceID and the
                # product's wire id as ParentID; they never reach a Win so they
                # never fire g_Done.
                if sp.get("final"):
                    piece_id = parent_id = wire
                else:
                    piece_id = next_piece_id()
                    parent_id = wire
                # Use the shared cell handshake in the PLC client (the
                # single source of truth: wait head free -> write + recv
                # -> wait accepted -> drop recv -> wait head ready),
                # instead of re-implementing the protocol here.
                try:
                    ok = await self.plc.send_workpiece_handshake(
                        cell, sp["raw"], sp["ops"],
                        piece_id=piece_id, parent_id=parent_id)
                except Exception as e:
                    print(f"[disp] subpart {idx} handshake error: {e}")
                    ok = False
                if not ok:
                    print(f"[disp] chunk aborted at subpart {idx}")
                    return
                # First subpart accepted -> the piece physically entered the
                # cell. Stamp cell_started_at so timing excludes queue wait.
                if idx == 1:
                    try:
                        mark_cell_started(chunk_rows[0]["id"],
                                          datetime.now(timezone.utc))
                    except Exception as e:
                        print(f"[disp] mark_cell_started error: {e}")
                # NOTE: the tool tracker is advanced at dispatch (in tick), not
                # here, so dispatched_ops can be persisted up front (TASK 1).
            print(f"[disp] {chunk_rows[0]['piece_type']} all subparts sent "
                  f"to Cell_{cell}")
        finally:
            async with self._busy_lock:
                self._busy_cells.discard(cell)

# ============================================================================
# Complex (mixed-material) pieces: RWM / SWM
# ============================================================================
# A complex piece needs a wood top + two metal legs. No single cell can shape
# both wood and metal on its M1/M2 machines, so the build is staged:
#
#   Phase A  In parallel:
#              * shape both metal legs INSIDE the assembly cell (a
#                metal+assembly cell, 3 or 4). Leg 1 -> M2, leg 2 -> M1 so
#                they shape at the same time; each carries a trailing no-op at
#                M3 so it parks in the assembly buffer and never leaves.
#              * shape the wood top in a wood cell (1 or 2); it exits to W2.
#   Phase B  The PLC Router corridor brings ONLY the top from W2 to W1
#            (driven entirely PLC-side; the MES does not touch Transfer_*).
#   Phase C  Re-inject the top into the assembly cell at M3 with tool 9. The
#            two parked legs are consumed; the finished product exits to W2.
#   Phase D  Mark the order complete and report it. The finished product
#            STAYS in W2 (v3 Bug 6) — no W2->W1 return.
#
# Only the top crosses the corridor; the legs stay in the assembly cell.


class ComplexOrchestrator:
    def __init__(self, plc, mqtt_bridge, publish_status,
                 busy_cells: set, busy_lock: asyncio.Lock,
                 tool_tracker=None):
        self.plc = plc
        self.mqtt = mqtt_bridge
        self.publish_status = publish_status
        self.tools = tool_tracker
        # Shared with the Dispatcher so simple and complex production
        # never claim the same physical cell at once.
        self._busy_cells = busy_cells
        self._busy_lock = busy_lock
        self._inflight = 0       # number of complex products in flight
        # Per in-flight product: wire id -> assembly cell held, released on
        # completion. A dict (not a single var) because several run in parallel.
        self._held_cell = {}
        # The PLC g_Router_Order is a SINGLE intake slot. With parallel
        # products, two _produce tasks would write it at once and clobber each
        # other. This lock serialises the intake handshake.
        self._router_lock = asyncio.Lock()
        # Wood-cell top shaping spreads across M2 and M1 of the wood cell: the
        # first top of a pair shapes on M2, the second on M1, so two tops shape
        # in parallel (M1 is upstream of M2 in the cell, so a top at M1 and a
        # top at M2 run concurrently) instead of serialising on M1. The machine
        # is picked under _router_lock so it follows the actual push order.
        self._top_machines = [2, 1]
        self._top_rr = 0
        # Round-robin the wood SOURCE cell of the top across both wood cells
        # (C1, C2) so two assembly cells aren't both fed by one wood cell
        # (TASK 5). The corridor transfer that carries the top W2->W1 stays
        # serialised PLC-side (and via _router_lock here), so spreading the
        # SHAPING source is safe — it does not raise the number of concurrent
        # corridor transfers. Picked under _router_lock so it follows the
        # actual push order.
        self._top_cell_rr = 0

    def _next_top_machine(self) -> int:
        m = self._top_machines[self._top_rr % len(self._top_machines)]
        self._top_rr += 1
        return m

    def _next_top_cell(self, cells) -> int:
        c = cells[self._top_cell_rr % len(cells)]
        self._top_cell_rr += 1
        return c

    # ---- helpers --------------------------------------------------------

    @staticmethod
    def _raw_need(rec) -> dict:
        need = {}
        need[rec["top"]["raw"]] = need.get(rec["top"]["raw"], 0) + 1
        need[rec["leg"]["raw"]] = (need.get(rec["leg"]["raw"], 0)
                                   + rec["leg"]["count"])
        return need

    def _can_afford(self, rec) -> bool:
        est = self.mqtt.w1_estimate()
        return all(est.get(k, 0) >= v for k, v in self._raw_need(rec).items())

    async def _claim_cell(self, cells, timeout=60.0):
        """Reserve the first free cell from `cells` (PLC-free and not held
        by the dispatcher or us). Returns the cell number or None."""
        step = 0.2
        for _ in range(max(1, int(timeout / step))):
            async with self._busy_lock:
                for c in cells:
                    if c in self._busy_cells:
                        continue
                    try:
                        if await self.plc.cell_free(c):
                            self._busy_cells.add(c)
                            return c
                    except Exception:
                        continue
            await asyncio.sleep(step)
        return None

    def _release_cell(self, cell):
        if cell is not None:
            self._busy_cells.discard(cell)

    # ---- main loop ------------------------------------------------------

    async def run_loop(self):
        while True:
            try:
                await self._tick()
            except Exception as e:
                print(f"[complex] tick error: {e}")
            await asyncio.sleep(1.0)

    async def _tick(self):
        # Parallel complex production: allow as many in flight as there are
        # assembly cells (Cell_3, Cell_4). Tops are all shaped in the single
        # wood cell; only the assembly cells run in parallel.
        asm_pool = COMPLEX_RECIPE.get("RWM", {}).get("asm", {}).get(
            "cells", [3, 4])
        if self._inflight >= len(asm_pool):
            return
        pending = queued_pieces()
        todo = [r for r in pending if r["piece_type"] in COMPLEX_PIECES]
        if not todo:
            return

        row = todo[0]
        rec = COMPLEX_RECIPE[row["piece_type"]]
        if not self._can_afford(rec):
            return  # wait for raw material to arrive in W1

        # Claim the order so neither the dispatcher nor the next tick
        # picks it up again. Allocate one INT16-safe wire id for the product.
        wire = next_piece_id()
        mark_dispatched(row["id"], cell=0, init_piece_id=rec["top"]["id"],
                        wire_piece_id=wire)
        self._inflight += 1
        asyncio.create_task(self._produce(row, rec, wire))

    # ---- production pipeline -------------------------------------------

    async def _produce(self, row, rec, wire):
        """Form A: build the routes for a complex piece and push them to the
        PLC-side Router, which carries each piece through cells + corridor on
        its own.

        Three routes per complex product:
          * leg 1: Metal -> asm_cell M2 (shape), then park at M3 (no-op)
          * leg 2: Metal -> asm_cell M1 (shape), then park at M3 (no-op)
          * top  : Wood -> top_cell M2/M1 (shape), then TRANSFER (Cell 0,
                   PieceArg = shaped top id), then assemble at asm_cell M3.

        `wire` is the INT16-safe wire id (from the shared generator) used as
        the product's PieceID/ParentID; completion is reported asynchronously
        by the PLC completion buffer (g_Done_*) keyed on it, consumed by the
        MES completion_loop — NOT marked here.
        """
        pt = row["piece_type"]
        pid = row["id"]
        top, leg, asm = rec["top"], rec["leg"], rec["asm"]
        print(f"[complex] start {pt} (db id {pid}, wire {wire}) "
              f"-> routing to PLC Router")

        # Choose a FREE assembly cell from the pool so two RWM do not collide.
        asm_cell = await self._claim_cell(asm["cells"])
        if asm_cell is None:
            print(f"[complex] {pt}: no free assembly cell in {asm['cells']}; "
                  f"will retry next tick")
            self._inflight = max(0, self._inflight - 1)
            return
        # Wood cell that shapes the top (shaping only; frees quickly). Chosen
        # under _router_lock below so the C1/C2 round-robin follows push order.

        # Per-op statistics records collected as routes are pushed, persisted
        # on the piece's row so they commit to the stats tables on completion
        # (TASK 1). For a complex piece the histogram is: 2x LegM on the
        # assembly cell's shaping machines, 1x top (RtopW/StopW) on the wood
        # cell, and 1x final product (RWM/SWM) on the assembly cell's M3.
        op_records = []
        try:
            # Reserve raw material in the local W1 model up front.
            for material, qty in self._raw_need(rec).items():
                self.mqtt.w1_consume(material, qty)

            # --- Route for each leg: shape on its machine, then no-op park at
            #     M3 so it waits in the assembly buffer. Single-cell routes. ---
            for i, m in enumerate(leg["machines"][:leg["count"]], 1):
                leg_ops = [
                    {"cell": asm_cell, "machine": m,
                     "tool": leg["tool"], "op_time_s": leg["time_s"],
                     "piece_arg": 0, "out": leg["piece"]},
                    {"cell": asm_cell, "machine": M3,
                     "tool": 0, "op_time_s": 0, "piece_arg": 0},
                ]
                print(f"[complex] {pt}: route leg {i} -> Cell_{asm_cell} "
                      f"M{m} park@M3")
                async with self._router_lock:
                    await self.plc.router_send_route(
                        init_piece=leg["raw_id"], operations=leg_ops,
                        piece_id=next_piece_id(), parent_id=wire)
                if self.tools:
                    op_records.extend(self.tools.apply_ops(asm_cell, leg_ops))

            # --- Route for the top: shape, transfer W2->W1, assemble. The wood
            #     cell (C1/C2), the machine (M2/M1) and the push are decided
            #     together under the lock so the round-robins stay deterministic
            #     and follow the actual corridor push order (TASK 5). ---
            async with self._router_lock:
                top_cell = self._next_top_cell(top["cells"])
                top_machine = self._next_top_machine()
                top_ops = [
                    {"cell": top_cell, "machine": top_machine,
                     "tool": top["tool"], "op_time_s": top["time_s"],
                     "piece_arg": 0, "out": top["piece"]},
                    {"cell": 0, "machine": 0, "tool": 0, "op_time_s": 0,
                     "piece_arg": top["id"]},          # transfer: pull shaped top
                    {"cell": asm_cell, "machine": asm["machine"],
                     "tool": asm["tool"], "op_time_s": asm["time_s"],
                     "piece_arg": 0, "out": pt},        # M3 assembles final product
                ]
                print(f"[complex] {pt}: route top -> Cell_{top_cell} "
                      f"M{top_machine} shape, transfer, assemble@Cell_{asm_cell}")
                await self.plc.router_send_route(
                    init_piece=top["raw_id"], operations=top_ops,
                    piece_id=wire, parent_id=wire)  # final product keyed by wire
                if self.tools:
                    # apply_ops filters by cell, so each call records only the
                    # ops that ran on that cell (top shaping on top_cell, the
                    # M3 assembly on asm_cell).
                    op_records.extend(self.tools.apply_ops(top_cell, top_ops))
                    op_records.extend(self.tools.apply_ops(asm_cell, top_ops))

            # Persist the per-op records on the piece's row (it was already
            # mark_dispatched in _tick; the routes — hence the cells — are only
            # known now) so completion_loop can commit them to the stats tables.
            try:
                set_dispatched_ops(pid, op_records)
            except Exception as e:
                print(f"[complex] set_dispatched_ops({pid}) failed: {e}")

            print(f"[complex] {pt} (db id {pid}, wire {wire}) fully routed "
                  f"to PLC; completion will arrive via g_Done buffer")
            # HOLD this product's assembly cell until the PLC reports it done
            # (on_complete releases it). Several may be held at once.
            self._held_cell[wire] = asm_cell
            return                      # do NOT release here
        except Exception as e:
            print(f"[complex] {pt}: routing error: {e}")
        # Only reached on error: release immediately so we don't wedge.
        self._release_cell(asm_cell)
        self._inflight = max(0, self._inflight - 1)

    async def on_complete(self, parent_id: int):
        """Called by the MES completion_loop when the PLC reports a finished
        product. Drives the stale re-injection recv_cmd on the held assembly
        cell low, releases that cell and frees one in-flight slot. Ignores
        completions for products we are not tracking.

        MES band-aid for the CODESYS defect in §7.1: the corridor's
        Dispatch_At_Wh1 re-injects the transferred top by RAISING
        Cell_c_Top_Order.recv_cmd but never lowers it. With recv_cmd stuck
        high the assembly cell's Wout can never finish ugly_hack -> free, so
        the cell reads busy forever and its (now finished-product) workpiece
        data keeps re-entering W2, re-firing g_Done -> duplicate completions.
        Driving recv_cmd low here closes that handshake. (The proper fix lives
        in CODESYS — see the change note.)"""
        asm_cell = self._held_cell.pop(parent_id, None)
        if asm_cell is None:
            return
        try:
            await self.plc.clear_recv_cmd(asm_cell)
        except Exception as e:
            print(f"[complex] clear_recv_cmd(Cell_{asm_cell}) failed: {e}")
        self._release_cell(asm_cell)
        self._inflight = max(0, self._inflight - 1)
        print(f"[complex] parent={parent_id} done; Cell_{asm_cell} recv_cmd "
              f"cleared + released, inflight={self._inflight}")