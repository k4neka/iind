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

from config import NUM_CELLS, COMPLEX_PIECES, COMPLEX_RECIPE
from database import (queued_pieces, mark_dispatched, mark_completed)
from optimizer import optimise_batch
from transformations import ID_TO_PIECE

# Machine slot used as the in-cell assembly buffer / assembly station for
# complex pieces.
M3 = 3


class Dispatcher:
    def __init__(self, plc, mqtt_bridge, busy_cells=None, busy_lock=None):
        self.plc = plc
        self.mqtt = mqtt_bridge
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
        """Return the first cell in `capable` that is both PLC-free
        and not held by another in-flight chunk. `target_hint` is
        tried first to honour the optimiser's preference."""
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
            pending, rr_offset_by_family=self._family_rr
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
            for row in chunk_rows:
                mark_dispatched(row["id"], cell, subparts[0]["raw"])
            print(f"[disp] chunk of {len(chunk_rows)} "
                  f"({chunk_rows[0]['piece_type']}) -> Cell_{cell} "
                  f"as {len(subparts)} subparts "
                  f"(hint was Cell_{target_cell})")

            asyncio.create_task(
                self._run_chunk(chunk_rows, cell, subparts)
            )

    # ---- subpart send loop ---------------------------------------------

    async def _run_chunk(self, chunk_rows, cell, subparts):
        try:
            for idx, sp in enumerate(subparts, start=1):
                # Use the shared cell handshake in the PLC client (the
                # single source of truth: wait head free -> write + recv
                # -> wait accepted -> drop recv -> wait head ready),
                # instead of re-implementing the protocol here.
                try:
                    ok = await self.plc.send_workpiece_handshake(
                        cell, sp["raw"], sp["ops"])
                except Exception as e:
                    print(f"[disp] subpart {idx} handshake error: {e}")
                    ok = False
                if not ok:
                    print(f"[disp] chunk aborted at subpart {idx}")
                    return
            print(f"[disp] chunk of {len(chunk_rows)} "
                  f"({chunk_rows[0]['piece_type']}) all subparts sent "
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
#   Phase B  Use the Transfer_Cell to bring ONLY the top from W2 to W1.
#   Phase C  Re-inject the top into the assembly cell at M3 with tool 9. The
#            two parked legs are consumed; the finished product exits to W2.
#   Phase D  Mark the order complete, report it, and return the finished
#            product to W1 (via the shared TransferManager).
#
# Only the top crosses the corridor; the legs stay in the assembly cell.
# Complex pieces are processed one at a time so the single Transfer_Cell and
# the assembly-cell buffer never get crossed between two products.


class ComplexOrchestrator:
    def __init__(self, plc, mqtt_bridge, publish_status, transfer_manager,
                 busy_cells: set, busy_lock: asyncio.Lock):
        self.plc = plc
        self.mqtt = mqtt_bridge
        self.publish_status = publish_status
        self.transfers = transfer_manager
        # Shared with the Dispatcher so simple and complex production
        # never claim the same physical cell at once.
        self._busy_cells = busy_cells
        self._busy_lock = busy_lock
        self._inflight = 0       # number of complex products in flight
        # Unique PieceID source for sub-pieces (legs). Never 0 (0 = empty slot
        # in the PLC completion buffer). The PLC Workpiece_T.PieceID is INT
        # (16-bit signed: -32768..32767), so this MUST stay under 32767. We use
        # a band well above DB pids (which start at 1) but safely in range.
        self._piece_id_seq = 1000
        # Per in-flight product: parent_id -> assembly cell held, released on
        # completion. A dict (not a single var) because several run in parallel.
        self._held_cell = {}
        # The PLC g_Router_Order is a SINGLE intake slot. With parallel
        # products, two _produce tasks would write it at once and clobber each
        # other (the dropped-route WARN). This lock serialises the intake
        # handshake: each route completes its take-ACK before the next starts.
        # Production stays parallel on the floor; only the push is serialised.
        self._router_lock = asyncio.Lock()

    def _next_piece_id(self) -> int:
        self._piece_id_seq += 1
        if self._piece_id_seq > 30000:   # wrap, staying inside INT16
            self._piece_id_seq = 1001
        return self._piece_id_seq

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
        # assembly cells (Cell_3, Cell_4). _inflight counts products that have
        # claimed a cell and not yet completed. Tops are all shaped in the
        # single wood cell (serialised naturally there); only the assembly
        # cells run in parallel, so two RWM never fight over the corridor
        # (the wood cell hands their tops to W2 one at a time).
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
        # picks it up again.
        mark_dispatched(row["id"], cell=0, init_piece_id=rec["top"]["id"])
        self._inflight += 1
        asyncio.create_task(self._produce(row, rec))

    # ---- production pipeline -------------------------------------------

    async def _produce(self, row, rec):
        """Form A: build the routes for a complex piece and push them to the
        PLC-side Router, which carries each piece through cells + corridor on
        its own. The MES no longer orchestrates cells/corridor step by step.

        Three routes are emitted per complex product:
          * leg 1: Metal -> asm_cell M2 (shape), then park at M3 (no-op)
          * leg 2: Metal -> asm_cell M1 (shape), then park at M3 (no-op)
          * top  : Wood -> top_cell M1 (shape), then TRANSFER (Cell 0,
                   PieceArg = shaped top id), then assemble at asm_cell M3.

        Completion is reported asynchronously by the PLC completion buffer
        (g_Done_*), consumed by the MES completion_loop — NOT marked here.
        """
        pt = row["piece_type"]
        pid = row["id"]
        top, leg, asm = rec["top"], rec["leg"], rec["asm"]
        print(f"[complex] start {pt} (db id {pid}) -> routing to PLC Router")

        # Choose a FREE assembly cell from the pool (e.g. [3, 4]) so two RWM
        # do not collide on the same Cell_3. We claim it for the whole product
        # (legs + assembly happen there) and release it on completion. The top
        # is always shaped in a single wood cell (Cell_1); only the assembly
        # cell alternates, exactly as described: 2 legs to Cell_3/Cell_4,
        # tops via Cell_1, then to different assembly cells.
        asm_cell = await self._claim_cell(asm["cells"])
        if asm_cell is None:
            print(f"[complex] {pt}: no free assembly cell in {asm['cells']}; "
                  f"will retry next tick")
            self._inflight = max(0, self._inflight - 1)
            return
        top_cell = top["cells"][0]   # wood cell (shaping only; frees quickly)

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
                     "piece_arg": 0},
                    {"cell": asm_cell, "machine": M3,
                     "tool": 0, "op_time_s": 0, "piece_arg": 0},
                ]
                print(f"[complex] {pt}: route leg {i} -> Cell_{asm_cell} "
                      f"M{m} park@M3")
                async with self._router_lock:
                    await self.plc.router_send_route(
                        init_piece=leg["raw_id"], operations=leg_ops,
                        piece_id=self._next_piece_id(), parent_id=pid)

            # --- Route for the top: shape, transfer W2->W1, assemble. The
            #     transfer op carries the shaped-top id in piece_arg so the
            #     corridor pulls the right piece out of W2. ---
            top_ops = [
                {"cell": top_cell, "machine": top["machine"],
                 "tool": top["tool"], "op_time_s": top["time_s"],
                 "piece_arg": 0},
                {"cell": 0, "machine": 0, "tool": 0, "op_time_s": 0,
                 "piece_arg": top["id"]},          # transfer: pull shaped top
                {"cell": asm_cell, "machine": asm["machine"],
                 "tool": asm["tool"], "op_time_s": asm["time_s"],
                 "piece_arg": 0},
            ]
            print(f"[complex] {pt}: route top -> Cell_{top_cell} shape, "
                  f"transfer, assemble@Cell_{asm_cell}")
            async with self._router_lock:
                await self.plc.router_send_route(
                    init_piece=top["raw_id"], operations=top_ops,
                    piece_id=pid, parent_id=pid)   # final product keyed by pid

            print(f"[complex] {pt} (db id {pid}) fully routed to PLC; "
                  f"completion will arrive via g_Done buffer")
            # Parallel: HOLD this product's assembly cell until the PLC reports
            # it done (on_complete releases it). Several products may be held at
            # once, each under its own parent id.
            self._held_cell[pid] = asm_cell
            return                      # do NOT release here
        except Exception as e:
            print(f"[complex] {pt}: routing error: {e}")
        # Only reached on error: release immediately so we don't wedge.
        self._release_cell(asm_cell)
        self._inflight = max(0, self._inflight - 1)

    def on_complete(self, parent_id: int):
        """Called by the MES completion_loop when the PLC reports a finished
        product. Releases that product's held assembly cell and frees one
        in-flight slot. Matches by parent id; ignores completions for products
        we are not tracking (e.g. simple pieces, which never registered here)."""
        if parent_id in self._held_cell:
            self._release_cell(self._held_cell.pop(parent_id))
            self._inflight = max(0, self._inflight - 1)
            print(f"[complex] parent={parent_id} done; cell released, "
                  f"inflight={self._inflight}")