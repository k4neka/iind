"""MES unit tests (piece_id, tool_state, transformations, optimizer, config,
mqtt queue cap). Run: python3 tests/run_all.py  (or python3 tests/test_mes.py)."""
import io
import contextlib
import unittest

import _stubs


def _quiet(fn, *a, **k):
    with contextlib.redirect_stdout(io.StringIO()):
        return fn(*a, **k)


class PieceIdTests(unittest.TestCase):
    def setUp(self):
        _stubs.use_mes_path()
        import importlib
        self.pid = importlib.reload(importlib.import_module("piece_id"))

    def test_monotonic_never_zero(self):
        ids = [self.pid.next_piece_id() for _ in range(5)]
        self.assertEqual(ids, [1, 2, 3, 4, 5])
        self.assertTrue(all(0 < i <= 30000 for i in ids))

    def test_wraps_inside_int16(self):
        self.pid._counter = 30000
        self.assertEqual(self.pid.next_piece_id(), 1)   # wrap, skips 0


class ToolStateTests(unittest.TestCase):
    def setUp(self):
        _stubs.use_mes_path()
        import importlib
        self.ts = importlib.import_module("tool_state")

    def test_init_from_defaults(self):
        t = self.ts.ToolStateTracker()
        self.assertEqual(t.snapshot(1)[1], 1)
        self.assertEqual(t.snapshot(3)[1], 4)
        self.assertEqual(t.snapshot(1)[3], 8)

    def test_apply_ops_updates_only_real_tools(self):
        t = self.ts.ToolStateTracker()
        t.apply_ops(1, [{"cell": 1, "machine": 1, "tool": 3},
                        {"cell": 1, "machine": 3, "tool": 0}])  # 0 = no-op
        self.assertEqual(t.snapshot(1)[1], 3)
        self.assertEqual(t.snapshot(1)[3], 8)                   # untouched
        # apply_ops(cell, ops) only applies ops whose op["cell"] == cell.
        t.apply_ops(1, [{"cell": 2, "machine": 1, "tool": 5}])  # cell mismatch
        self.assertEqual(t.snapshot(2)[1], 1)                   # cell 2 untouched
        t.apply_ops(2, [{"cell": 2, "machine": 1, "tool": 5}])  # correct cell
        self.assertEqual(t.snapshot(2)[1], 5)

    def test_snapshots(self):
        t = self.ts.ToolStateTracker()
        self.assertEqual(t.snapshot(1), {1: 1, 2: 1, 3: 8})
        alls = t.all_cells_snapshot()
        self.assertEqual(set(alls.keys()), {1, 2, 3, 4})

    def test_reset(self):
        t = self.ts.ToolStateTracker()
        t.apply_ops(1, [{"cell": 1, "machine": 1, "tool": 3}])
        t.reset()
        self.assertEqual(t.snapshot(1)[1], 1)


class TransformationsTests(unittest.TestCase):
    def setUp(self):
        _stubs.use_mes_path()
        import importlib
        self.tr = importlib.import_module("transformations")

    def test_piece_id_roundtrip(self):
        for name, pid in self.tr.PIECE_ID.items():
            self.assertEqual(self.tr.ID_TO_PIECE[pid], name)

    def test_find_single_and_assembly(self):
        self.assertTrue(self.tr.find_single("RtopW"))
        a = self.tr.find_assembly("RWM")
        self.assertEqual((a["top"], a["leg"], a["tool"]), ("RtopW", "LegM", 9))
        self.assertIsNone(self.tr.find_assembly("Wood"))

    def test_raw_for(self):
        self.assertEqual(self.tr.raw_for("Wood"), "Wood")
        self.assertEqual(self.tr.raw_for("RtopW"), "Wood")
        self.assertEqual(self.tr.raw_for("LegM"), "Metal")
        self.assertIsNone(self.tr.raw_for("nope"))


class OptimizerTests(unittest.TestCase):
    def setUp(self):
        _stubs.use_mes_path()
        import importlib
        self.opt = importlib.import_module("optimizer")

    def test_capable_cells(self):
        self.assertEqual(sorted(self.opt.capable_cells("RWW")), [1, 2])
        self.assertEqual(sorted(self.opt.capable_cells("RMM")), [3, 4])
        self.assertEqual(self.opt.capable_cells("RWM"), [])   # mixed -> none

    def test_cell_timeline_tool_seed_and_change(self):
        tl = self.opt.CellTimeline(tool_state={1: {1: 3, 2: 1, 3: 8}})
        self.assertEqual(tl.tool[1][1], 3)
        # cost_to_run returns (start_after_change, finish); the tool change is
        # added to `start`, the duration is finish-start.
        s, f = tl.cost_to_run(1, 1, 3, 10)      # tool 3 already on slot 1
        self.assertEqual(s, 0)                  # no change
        self.assertEqual(f - s, 10)
        s, f = tl.cost_to_run(1, 2, 9, 10)      # slot 2 has tool 1 -> change
        self.assertEqual(s, self.opt.TOOL_CHANGE_TIME)

    def test_optimise_batch_one_product_per_chunk(self):
        queue = [{"id": 1, "piece_type": "RWW"}, {"id": 2, "piece_type": "RWW"}]
        chunks, rr = self.opt.optimise_batch(queue)
        self.assertEqual(len(chunks), 2)                  # one product each
        for rows, cell, caps, sps in chunks:
            self.assertEqual(len(rows), 1)
            legs = [s for s in sps if not s.get("final")]
            tops = [s for s in sps if s.get("final")]
            self.assertEqual(len(legs), 2)                # 2 parked legs
            self.assertEqual(len(tops), 1)                # 1 final top
            self.assertEqual(tops[0]["final_index"], 0)

    def test_optimise_batch_round_robin_spreads_cells(self):
        queue = [{"id": i, "piece_type": "RWW"} for i in range(1, 5)]
        chunks, _ = self.opt.optimise_batch(queue)
        cells = {c for _, c, _, _ in chunks}
        self.assertTrue({1, 2}.issubset(cells))           # spread across 1 and 2

    def test_optimise_batch_skips_complex(self):
        chunks, _ = self.opt.optimise_batch([{"id": 1, "piece_type": "RWM"}])
        self.assertEqual(chunks, [])


class StatisticsOpsTests(unittest.TestCase):
    """Dispatch-side op records that feed the statistics recorder (TASK 1):
    each op's OUTPUT piece is attributed to the machine that ran it, and
    tool_change is counted against the tool mounted just before the op."""

    def setUp(self):
        _stubs.use_mes_path()
        import importlib
        self.opt = importlib.import_module("optimizer")
        self.ts = importlib.import_module("tool_state")

    def test_optimizer_ops_carry_output_piece(self):
        chunks, _ = _quiet(self.opt.optimise_batch,
                           [{"id": 1, "piece_type": "RWW"}])
        _rows, _cell, _caps, subparts = chunks[0]
        legs = [s for s in subparts if not s.get("final")]
        top = [s for s in subparts if s.get("final")][0]
        # Leg shaping ops output LegW; the M3 park op (tool 0) has no output.
        for leg in legs:
            self.assertEqual(leg["ops"][0]["out"], "LegW")
            self.assertNotIn("out", leg["ops"][1])
        # Top shaping outputs RtopW; M3 assembly outputs the FINAL product RWW
        # (not attributed to the shaping machines).
        self.assertEqual(top["ops"][0]["out"], "RtopW")
        self.assertEqual(top["ops"][1]["out"], "RWW")

    def test_apply_ops_emits_records_and_counts_changes(self):
        t = self.ts.ToolStateTracker()        # in-memory; cell1 M1=1,M2=1,M3=8
        chunks, _ = _quiet(self.opt.optimise_batch,
                           [{"id": 1, "piece_type": "RWW"}])
        _rows, cell, _caps, subparts = chunks[0]
        records = []
        for sp in subparts:
            records.extend(t.apply_ops(cell, sp["ops"]))
        # One record per real op: 2 leg shaping + 1 top shaping + 1 assembly.
        self.assertEqual(len(records), 4)
        self.assertEqual(sorted(r["out"] for r in records),
                         ["LegW", "LegW", "RWW", "RtopW"])
        self.assertTrue(all(r["tool"] != 0 and r["time_s"] > 0
                            for r in records))
        # M3 assembly uses tool 8, already mounted at startup -> no change.
        asm = next(r for r in records if r["out"] == "RWW")
        self.assertEqual(asm["slot"], 3)
        self.assertFalse(asm["tool_change"])
        # The optimiser's sticky-tool logic keeps both legs on the same machine
        # (reusing T3) and shapes the top on the M1 that already holds T1, so a
        # cold RWW causes exactly ONE real tool change (the first leg, T1->T3).
        # tool_changes must equal the switches the optimiser actually caused.
        self.assertEqual(sum(1 for r in records if r["tool_change"]), 1)


class MesQueueCapTests(unittest.TestCase):
    def setUp(self):
        self.db = _stubs.install_fake_mes_db()
        import importlib
        self.mc = importlib.import_module("mqtt_client")
        self.cfg = importlib.import_module("config")

    def test_production_orders_queue_and_cap(self):
        bridge = self.mc.MESMqtt(loop=None, plc=None)
        # Under the cap: all queued.
        _quiet(bridge._handle_production_orders,
               {"items": [{"piece_type": "RWW", "quantity": 5,
                           "order_line_id": 1}]})
        self.assertEqual(len(self.db.queue), 5)
        # Push past MAX_QUEUED (16): extra dropped.
        _quiet(bridge._handle_production_orders,
               {"items": [{"piece_type": "RWW", "quantity": 50,
                           "order_line_id": 2}]})
        self.assertEqual(len(self.db.queue), self.cfg.MAX_QUEUED)


class _FakePLC:
    """Minimal async PLC stub exposing only the dock reads the unloader uses
    during load/seed."""
    def __init__(self, counts):
        self._counts = dict(counts)

    async def read_all_dock_counts(self):
        return dict(self._counts)

    async def read_dock_count(self, dock):
        return self._counts.get(dock, 0)


class UnloaderPersistenceTests(unittest.TestCase):
    """TASK 4: the unloader books survive a mid-day MES restart and a stale
    owner (dock the PLC shows empty) is cleared on reload."""

    def setUp(self):
        self.db = _stubs.install_fake_mes_db()
        import importlib
        self.un = importlib.import_module("unloader")

    def test_books_survive_restart(self):
        import asyncio
        plc = _FakePLC({1: 0, 2: 2, 3: 0, 4: 0, 5: 0})
        um1 = self.un.UnloaderManager(plc=plc)
        # A half-built line on dock 2, plus one spare RWW credited to W2.
        um1._lines = {7: {"client": "C", "order": 99, "piece_type": "RWW",
                          "ddate": 5, "target": 4, "need": 2, "on_docks": 2,
                          "docks": {2}}}
        um1._w2_stock = {"RWW": 1}
        um1._dock_owner[2] = 7
        um1._dock_count[2] = 2
        _quiet(um1._persist)
        # Restart: a fresh manager loads the same persisted books.
        um2 = self.un.UnloaderManager(plc=plc)
        _quiet(lambda: asyncio.run(um2._ensure_loaded()))
        self.assertIn(7, um2._lines)
        self.assertEqual(um2._lines[7]["on_docks"], 2)
        self.assertEqual(um2._lines[7]["docks"], {2})
        self.assertEqual(um2._w2_stock.get("RWW"), 1)
        self.assertEqual(um2._dock_owner[2], 7)        # ownership resumed
        self.assertEqual(um2._dock_count[2], 2)

    def test_empty_dock_clears_stale_owner(self):
        import asyncio
        plc = _FakePLC({1: 0, 2: 0, 3: 0, 4: 0, 5: 0})  # dock 2 now empty
        um1 = self.un.UnloaderManager(plc=plc)
        um1._lines = {7: {"client": "C", "order": 1, "piece_type": "RWW",
                          "ddate": 5, "target": 4, "need": 4, "on_docks": 0,
                          "docks": set()}}
        um1._dock_owner[2] = 7
        um1._dock_count[2] = 0
        _quiet(um1._persist)
        um2 = self.un.UnloaderManager(plc=plc)
        _quiet(lambda: asyncio.run(um2._ensure_loaded()))
        self.assertIsNone(um2._dock_owner[2])          # no pieces -> cleared


class MesConfigTests(unittest.TestCase):
    def test_constants(self):
        _stubs.use_mes_path()
        import importlib
        cfg = importlib.import_module("config")
        self.assertEqual(cfg.NUM_CELLS, 4)
        self.assertEqual(cfg.WAREHOUSE_CAPACITY, 32)
        self.assertEqual(cfg.MAX_QUEUED, 16)
        self.assertIn("RWM", cfg.COMPLEX_PIECES)
        self.assertFalse(hasattr(cfg, "PRODUCT_BATCH_SIZE"))   # removed (v3)


if __name__ == "__main__":
    unittest.main(verbosity=2)
