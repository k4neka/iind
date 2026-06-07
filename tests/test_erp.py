"""ERP unit + integration tests (planner, time_model, mqtt_client, tcp_server,
sim_clock, config). Run: python3 tests/run_all.py  (or python3 tests/test_erp.py)."""
import io
import json
import contextlib
import unittest

import _stubs


def _quiet(fn, *a, **k):
    with contextlib.redirect_stdout(io.StringIO()):
        return fn(*a, **k)


class TimeModelTests(unittest.TestCase):
    def setUp(self):
        self.db = _stubs.install_fake_erp_db()
        import importlib
        self.tm = importlib.import_module("time_model")

    def test_estimate_production_seconds_simple(self):
        self.assertGreater(self.tm.estimate_production_seconds("RWW"), 0)
        self.assertEqual(self.tm.estimate_production_seconds("NOPE"), 0.0)

    def test_estimate_production_days_min1(self):
        self.assertGreaterEqual(self.tm.estimate_production_days("RWW"), 1)

    def test_machine_tool_state_change_cost(self):
        st = self.tm.MachineToolState(1, 8)
        self.assertEqual(st.use("M1", 1), 0.0)          # already mounted
        self.assertEqual(st.use("M1", 3), self.tm.TOOL_CHANGE_TIME_S)  # change
        self.assertEqual(st.use("M1", 3), 0.0)          # now mounted

    def test_mixed_estimate_and_warm(self):
        cold = self.tm._estimate_mixed(self.tm.RECIPES["RWM"], False, warm=False)
        warm = self.tm._estimate_mixed(self.tm.RECIPES["RWM"], False, warm=True)
        self.assertGreater(cold, 0)
        self.assertLessEqual(warm, cold)

    def test_static_with_tools_cold_ge_warm(self):
        cold = self.tm._static_with_tools("RWW", None)
        warm = self.tm._static_with_tools("RWW", self.tm._own_tool_map("RWW"))
        self.assertGreaterEqual(cold, warm)

    def test_own_tool_map(self):
        m = self.tm._own_tool_map("RWW")
        self.assertEqual(set(m.keys()), {1, 2, 3})
        self.assertIsNone(self.tm._own_tool_map("RWM"))  # mixed -> None

    def test_read_timing_stats_pooled(self):
        self.db.sim_state["timing:RWW:1"] = json.dumps({"mean_s": 100, "n": 6})
        self.db.sim_state["timing:RWW:2"] = json.dumps({"mean_s": 50, "n": 2})
        st = self.tm.read_timing_stats("RWW")
        self.assertEqual(st["n"], 8)
        self.assertAlmostEqual(st["mean_s"], (100 * 6 + 50 * 2) / 8)

    def test_expected_seconds_fallback_chain(self):
        # No samples -> static.
        self.assertEqual(self.tm.expected_seconds("RWW"),
                         self.tm.estimate_production_seconds("RWW"))
        # >=5 samples -> measured mean.
        self.db.sim_state["timing:RWW:1"] = json.dumps({"mean_s": 42, "n": 6})
        self.assertEqual(self.tm.expected_seconds("RWW", cell=1), 42)

    def test_expected_seconds_warm_credit(self):
        base = self.tm.estimate_production_seconds("RWW")
        warm = self.tm.expected_seconds("RWW", preceding_type="RWW")
        self.assertLess(warm, base)         # credit applied
        self.assertGreaterEqual(warm, 0.0)

    def test_expected_production_days(self):
        self.assertGreaterEqual(self.tm.expected_production_days("RMM"), 1)


class PipelineModelTests(unittest.TestCase):
    """TASK 3: the cell pipeline models the M2-behind-M1 coupling."""

    def setUp(self):
        self.db = _stubs.install_fake_erp_db()
        import importlib
        self.tm = importlib.import_module("time_model")

    def test_m2_behind_m1_coupling(self):
        pipe = self.tm.CellPipeline(hop_seconds=0)
        # Two earlier pieces left the M2 stage free at t=20.
        pipe.free_at["M2"] = 20.0
        # A new piece whose M1 job takes 30s, then a short M2 job.
        info = pipe.add(m1=30, m2=5, m3=0, arrival=0.0)
        self.assertEqual(info["M1"]["finish"], 30.0)
        # It starts at M2 only when M1 frees the pipeline slot (30s), NOT when
        # the M2 stage went idle (20s) -- the larger 30s gate, not 20s.
        self.assertEqual(info["M2"]["start"], 30.0)
        self.assertEqual(info["M2"]["wait"], 0.0)   # M1 path is the binding gate

    def test_m2_bound_waits_for_stage(self):
        # Opposite case: when M2 is busy past the M1 finish, the piece waits at
        # M2 and the wait is measured from its M1 finish.
        pipe = self.tm.CellPipeline(hop_seconds=0)
        pipe.free_at["M2"] = 50.0
        info = pipe.add(m1=30, m2=5, m3=0, arrival=0.0)
        self.assertEqual(info["M2"]["start"], 50.0)
        self.assertEqual(info["M2"]["wait"], 20.0)  # 50 - 30 (its M1 finish)

    def test_schedule_day_contention(self):
        finishes = self.tm.schedule_day(1, ["RWW", "RWW"])
        self.assertEqual(len(finishes), 2)
        self.assertGreater(finishes[1], finishes[0])   # 2nd waits behind 1st

    def test_deadline_pessimistic_and_optimistic(self):
        base = self.tm.expected_seconds("RWW")
        self.assertEqual(self.tm.deadline_finish_seconds("RWW"),
                         base + self.tm.TIME_TOLERANCE_S)
        self.assertEqual(self.tm.optimistic_seconds("RWW"),
                         max(0.0, base - self.tm.TIME_TOLERANCE_S))
        self.assertGreaterEqual(self.tm.deadline_finish_days("RWW"), 1)


class PlannerUnitTests(unittest.TestCase):
    def setUp(self):
        self.db = _stubs.install_fake_erp_db()
        import importlib
        self.pl = importlib.import_module("planner")

    def test_urgency_slack_based(self):
        tight = {"piece_type": "RWW", "penalty": 100, "ddate": 3}
        loose = {"piece_type": "RWW", "penalty": 100, "ddate": 25}
        self.assertGreater(self.pl._urgency(tight, 0),
                           self.pl._urgency(loose, 0))
        self.assertEqual(self.pl._urgency({"piece_type": "RWW", "ddate": None},
                                          0), 0.0)

    def test_pending_inbound_window(self):
        self.db.add_purchase_entry(0, 4, "SupplierB", "Metal", 8, 32)
        self.assertEqual(self.pl._pending_inbound("Metal", by_day=6), 8)
        self.assertEqual(self.pl._pending_inbound("Metal", by_day=3), 0)

    def test_project_w1_stock(self):
        self.db.inventory["Wood"] = 5
        self.db.add_purchase_entry(0, 2, "SupplierB", "Wood", 12, 24)
        proj = self.pl._project_w1_stock(0, {"Wood": 5, "Metal": 0})
        self.assertEqual(proj[0], 5)
        self.assertEqual(proj[2], 17)            # 5 + 12 arrival

    def test_place_tranched_no_deadlock(self):
        w1 = {d: 0 for d in range(0, 31)}
        _quiet(self.pl._place_tranched, "Wood", 30, 0, 30, 10, 5, 2, w1)
        ordered = sum(p["quantity"] for p in self.db.purchase_plan)
        self.assertGreaterEqual(ordered, 30)     # all demand placed, no deadlock

    def test_place_tranched_prefer_fast(self):
        w1 = {d: 0 for d in range(0, 31)}
        _quiet(self.pl._place_tranched, "Wood", 6, 0, 30, 30, 0, 1, w1,
               prefer_fast=True)
        # fastest supplier = SupplierA (lead 0) -> arrives day 0
        self.assertTrue(any(p["arrival_day"] == 0 and p["supplier"] == "SupplierA"
                            for p in self.db.purchase_plan))

    def test_choose_supplier_B_when_meets_deadline(self):
        name, info, batch, cost, pen, late = self.pl._choose_supplier(
            "Wood", 3, current_day=0, prod_day=5, ddate=10,
            penalty_per_day=5, prod_days=1)
        self.assertEqual(name, "SupplierB")
        self.assertEqual(pen, 0.0)

    def test_choose_supplier_cost_benefit_picks_A_on_big_penalty(self):
        name, *_ = self.pl._choose_supplier(
            "Wood", 3, current_day=0, prod_day=1, ddate=1,
            penalty_per_day=1000, prod_days=1)
        self.assertEqual(name, "SupplierA")

    def test_current_inventory_helpers(self):
        self.db.inventory.update({"Wood": 7, "Metal": 3, "RWW": 2})
        self.assertEqual(self.pl._current_raw_inventory(),
                         {"Wood": 7, "Metal": 3})
        self.assertEqual(self.pl._current_finished_inventory().get("RWW"), 2)

    def test_pick_delivery_day(self):
        from collections import defaultdict
        occ = defaultdict(int)
        self.assertEqual(self.pl._pick_delivery_day(3, occ, 30), 3)


class PlannerIntegrationTests(unittest.TestCase):
    """Full replan + day-by-day dispatch against the in-memory DB. These are
    the tests that catch the v3 failures (deadlock, idle machines, late start)."""
    def setUp(self):
        self.db = _stubs.install_fake_erp_db()
        import importlib
        self.pl = importlib.import_module("planner")

    def test_baseline_ordered_fast_arrives_day0(self):
        _quiet(self.pl.ensure_baseline_stock, 0)
        woods = [p for p in self.db.purchase_plan if p["material"] == "Wood"]
        self.assertTrue(woods)
        self.assertTrue(all(p["arrival_day"] == 0 for p in woods))   # day-0 buffer

    def test_big_wood_order_is_fully_ordered_not_deadlocked(self):
        self.db.add_order_line("RWW", 10, ddate=10, penalty=50)  # 30 wood
        _quiet(self.pl.replan, 0)
        wood = sum(p["quantity"] for p in self.db.purchase_plan
                   if p["material"] == "Wood")
        self.assertGreaterEqual(wood, 30)
        # 10 production rows scheduled (no FK / no deadlock abort).
        self.assertEqual(len([r for r in self.db.production_plan
                              if r["piece_type"] == "RWW"]), 10)

    def test_dispatch_only_material_covered(self):
        self.db.add_order_line("RWW", 10, ddate=10, penalty=50)
        _quiet(self.pl.replan, 0)
        b = _stubs.CollectBridge()
        # Day 0: only the baseline (12 wood, day-0) is on hand -> 4 RWW (12/3).
        _quiet(self.pl.dispatch_today, 0, b)
        self.assertTrue(b.production)
        n0 = sum(it["quantity"] for _, items in b.production for it in items)
        self.assertEqual(n0, 4)
        self.assertEqual(self.db.inventory.get("Wood"), 0)   # 12 - 4*3

    def test_rmm_reuses_baseline_metal_no_double_order(self):
        # Baseline metal (8) ordered fast (arrives day 0). RMM needs 6 metal.
        self.db.add_order_line("RMM", 2, ddate=8, penalty=50)   # 6 metal
        _quiet(self.pl.replan, 0)
        extra = [p for p in self.db.purchase_plan
                 if p["material"] == "Metal" and p["arrival_day"] > 0]
        self.assertEqual(extra, [])      # baseline covers it; no late re-order

    def test_replan_survives_bad_line_fk(self):
        # Schedule references a non-existent line -> per-entry guard skips it,
        # replan still completes (no crash).
        good = self.db.add_order_line("RWW", 1, ddate=10, penalty=10)
        self.db.production_plan  # noqa
        # Force a phantom prod_entry by tampering: monkeypatch add to raise once.
        real_add = self.db.add_production_entry
        calls = {"n": 0}

        def flaky(*a, **k):
            calls["n"] += 1
            if calls["n"] == 1:
                raise ValueError("FK: simulated")
            return real_add(*a, **k)
        self.db.add_production_entry = flaky
        _quiet(self.pl.replan, 0)        # must not raise
        self.assertTrue(True)

    def test_stock_first_fulfilment(self):
        self.db.inventory["RWW"] = 3
        self.db.add_order_line("RWW", 3, ddate=10, penalty=10)
        _quiet(self.pl.replan, 0)
        fs = [r for r in self.db.production_plan if r["from_stock"]]
        self.assertEqual(len(fs), 3)     # all 3 from stock, no production
        self.assertEqual(self.db.get_reserved("RWW"), 3)


class ERPInventorySyncTests(unittest.TestCase):
    def setUp(self):
        self.db = _stubs.install_fake_erp_db()
        import importlib
        self.mc = importlib.import_module("mqtt_client")

    def test_sync_adds_finished_once(self):
        b = self.mc.MQTTBridge()
        payload = json.dumps({"status": "COMPLETED", "piece_type": "RWW",
                              "piece_db_id": 7})
        _quiet(b._sync_inventory, payload)
        self.assertEqual(self.db.inventory.get("RWW"), 1)
        _quiet(b._sync_inventory, payload)              # dup ignored
        self.assertEqual(self.db.inventory.get("RWW"), 1)

    def test_sync_ignores_non_completed(self):
        b = self.mc.MQTTBridge()
        _quiet(b._sync_inventory, json.dumps({"status": "QUEUED",
                                              "piece_type": "RWW"}))
        self.assertEqual(self.db.inventory.get("RWW"), None)


class SimClockTests(unittest.TestCase):
    def setUp(self):
        self.db = _stubs.install_fake_erp_db()
        import importlib
        self.sc = importlib.import_module("sim_clock")

    def test_day_advances_with_time(self):
        import time
        clk = self.sc.SimClock()
        base = clk.current_day()
        clk._start_real -= self.sc.SECONDS_PER_DAY * 2 + 1
        self.assertEqual(clk.current_day(), base + 2)
        self.assertLess(clk.seconds_into_day(), self.sc.SECONDS_PER_DAY)


class TcpParsingTests(unittest.TestCase):
    def setUp(self):
        self.db = _stubs.install_fake_erp_db()
        import importlib
        self.ts = importlib.import_module("tcp_server")

    def _run(self, payload_obj):
        sent = {}

        class FakeConn:
            def settimeout(self, t): pass
            def recv(self, n):
                if not getattr(self, "_done", False):
                    self._done = True
                    return json.dumps(payload_obj).encode()
                return b""
            def sendall(self, data): sent["resp"] = json.loads(data.decode())
            def close(self): pass

        clock = type("C", (), {"current_day": lambda self: 0})()
        fired = {"n": 0}
        srv = self.ts.OrderServer(clock, lambda d: fired.__setitem__("n", fired["n"] + 1))
        _quiet(srv._handle_client, FakeConn(), ("127.0.0.1", 1))
        return sent.get("resp"), fired["n"]

    def test_valid_order_accepted(self):
        resp, fired = self._run({"name": "X", "NIF": 1, "OrderID": 1,
                                 "orders": [{"type": "RWW", "quantity": 2,
                                             "DDate": 5, "Penalty": 10}]})
        self.assertEqual(resp["accepted"], 1)
        self.assertEqual(fired, 1)
        self.assertEqual(len(self.db.order_lines), 1)

    def test_bad_product_rejected(self):
        resp, _ = self._run({"name": "X", "NIF": 1, "OrderID": 2,
                             "orders": [{"type": "NOPE", "quantity": 1,
                                         "DDate": 5, "Penalty": 10}]})
        self.assertEqual(resp["accepted"], 0)
        self.assertTrue(resp["rejected"])


class ConfigTests(unittest.TestCase):
    def test_erp_config_constants(self):
        _stubs.use_erp_path()
        import importlib
        cfg = importlib.import_module("config")
        self.assertEqual(cfg.WAREHOUSE_CAPACITY, 32)
        self.assertIn("RWW", cfg.FINAL_PRODUCTS)
        self.assertIn("SupplierA", cfg.SUPPLIERS)
        self.assertEqual(cfg.SUPPLIERS["SupplierA"]["Wood"]["lead"], 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
