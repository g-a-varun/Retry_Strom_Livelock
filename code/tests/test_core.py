import io
import json
import os
import shutil
import sys
import tarfile
import tempfile
import unittest

CODE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, CODE_DIR)

import features
import labeling
import controller
import scenario_runner
import experiment_arms
import prediction_model
import evaluate_model
import calibrate_from_trace
from common import retry_policy, safe_files, telemetry, retry_client, fault_injection

# Run all tests from the code/ folder with:
#     python -m unittest discover tests -v

GW = ("gateway", "order_service")
PAY = ("order_service", "payment_service")
INV = ("order_service", "inventory_service")


def call(end, latency_ms, attempt, code, source="gateway", dest="order_service"):
    return {"timestamp": end, "start_time": end - latency_ms / 1000.0, "latency_ms": latency_ms,
            "attempt_number": attempt, "response_code": code,
            "source_service": source, "dest_service": dest}


def stats(n_calls=20, rho=0.0, s_useful=10.0, concurrency=1.0, p_success=1.0,
          lambda_retry=0.0, fail_ratio=0.0, overload_ratio=0.0, mean_latency_ms=60.0):
    return {"n_calls": n_calls, "rho": rho, "s_useful": s_useful, "concurrency": concurrency,
            "p_success": p_success, "lambda_retry": lambda_retry, "lambda_base": 5.0,
            "fail_ratio": fail_ratio, "overload_ratio": overload_ratio, "mean_latency_ms": mean_latency_ms}


class WindowStatsTests(unittest.TestCase):

    def test_empty_window(self):
        s = features.window_stats_for_edge([], 0.0, 1)[0]
        self.assertEqual(s["n_calls"], 0)
        self.assertEqual(s["rho"], 0.0)
        self.assertIsNone(s["p_success"])
        self.assertIsNone(s["mean_latency_ms"])
        self.assertEqual(s["fail_ratio"], 0.0)

    def test_counts_and_rho(self):
        records = [call(0.5, 100, 1, 200), call(0.6, 100, 1, 500), call(0.9, 100, 2, 503),
                   call(1.2, 100, 3, 0), call(1.5, 100, 1, 200)]
        s = features.window_stats_for_edge(records, 0.0, 1)[0]
        self.assertEqual(s["n_calls"], 5)
        self.assertAlmostEqual(s["rho"], 2.0 / 3.0)
        self.assertAlmostEqual(s["p_success"], 2.0 / 5.0)
        self.assertAlmostEqual(s["fail_ratio"], 1.0 / 3.0)      # 1 of 3 fresh calls failed
        self.assertAlmostEqual(s["overload_ratio"], 2.0 / 5.0)  # one 503 + one timeout
        self.assertAlmostEqual(s["s_useful"], 1.0)              # 2 successes / 2 s

    def test_only_retries_gives_max_rho(self):
        s = features.window_stats_for_edge([call(0.5, 50, 2, 500)], 0.0, 1)[0]
        self.assertEqual(s["rho"], features.RHO_MAX)

    def test_rho_is_capped(self):
        records = [call(0.1, 10, 1, 500)]
        for i in range(30):
            records.append(call(0.2 + i * 0.01, 10, 2, 500))
        s = features.window_stats_for_edge(records, 0.0, 1)[0]
        self.assertEqual(s["rho"], features.RHO_MAX)

    def test_concurrency_spreads_over_windows(self):
        # one call in flight from t=0.5 to t=3.0 (windows of 2 s from t=0)
        s = features.window_stats_for_edge([call(3.0, 2500, 1, 200)], 0.0, 2)
        self.assertAlmostEqual(s[0]["concurrency"], 1.5 / 2.0)
        self.assertAlmostEqual(s[1]["concurrency"], 1.0 / 2.0)
        self.assertEqual(s[0]["n_calls"], 0)   # counted where it ENDED
        self.assertEqual(s[1]["n_calls"], 1)

    def test_records_outside_range_are_ignored(self):
        s = features.window_stats_for_edge([call(-5.0, 10, 1, 200), call(50.0, 10, 1, 200)], 0.0, 2)
        self.assertEqual(s[0]["n_calls"] + s[1]["n_calls"], 0)


class ConditionTests(unittest.TestCase):

    def setUp(self):
        self.th = dict(labeling.DEFAULT_THRESHOLDS)
        self.base = {"s_useful": 20.0, "concurrency": 2.0, "latency_ms": 60.0}

    def test_healthy_window_is_not_livelock(self):
        now = stats(rho=0.05, s_useful=19.0, concurrency=2.0, lambda_retry=0.5)
        prev = stats(rho=0.05, s_useful=20.0, concurrency=2.0, lambda_retry=0.5)
        # C4 on its own is TRUE here: a steady trickle of retries is "not
        # falling". That is exactly why one condition is never enough and
        # all four must hold together.
        self.assertEqual(labeling.check_conditions(now, prev, self.base, self.th),
                         (False, False, False, True))

    def test_storm_window_meets_all_four(self):
        now = stats(rho=2.0, s_useful=1.0, concurrency=40.0, p_success=0.05, lambda_retry=20.0)
        prev = stats(rho=2.0, s_useful=1.0, concurrency=40.0, p_success=0.05, lambda_retry=21.0)
        self.assertEqual(labeling.check_conditions(now, prev, self.base, self.th),
                         (True, True, True, True))

    def test_recovering_storm_fails_condition_4(self):
        # retries clearly falling, success clearly improving -> damping, not livelock
        now = stats(rho=1.5, s_useful=5.0, concurrency=30.0, p_success=0.5, lambda_retry=8.0)
        prev = stats(rho=2.0, s_useful=1.0, concurrency=40.0, p_success=0.05, lambda_retry=20.0)
        c1, c2, c3, c4 = labeling.check_conditions(now, prev, self.base, self.th)
        self.assertFalse(c4)
        self.assertFalse(c3)   # concurrency fell by more than the noise tolerance

    def test_overload_without_retries_is_not_livelock(self):
        # the no-retry arm under a fault: useless and slow, but no retry loop
        now = stats(rho=0.0, s_useful=1.0, concurrency=40.0, p_success=0.05, lambda_retry=0.0)
        prev = stats(rho=0.0, s_useful=1.0, concurrency=40.0, p_success=0.05, lambda_retry=0.0)
        c1, c2, c3, c4 = labeling.check_conditions(now, prev, self.base, self.th)
        self.assertFalse(c1)
        self.assertFalse(c4)
        self.assertTrue(c2 and c3)

    def test_too_few_calls_are_not_judged(self):
        now = stats(n_calls=2, rho=5.0, s_useful=0.0, concurrency=40.0, p_success=0.0, lambda_retry=5.0)
        self.assertEqual(labeling.check_conditions(now, now, self.base, self.th),
                         (False, False, False, False))

    def test_no_history_means_no_condition_4(self):
        now = stats(rho=2.0, s_useful=1.0, concurrency=40.0, p_success=0.05, lambda_retry=20.0)
        self.assertFalse(labeling.check_conditions(now, None, self.base, self.th)[3])

    def test_flat_storm_within_noise_still_counts(self):
        # retries 10% lower than 4 s ago is noise (tolerance 20%), not recovery
        now = stats(rho=2.0, s_useful=1.0, concurrency=36.0, p_success=0.06, lambda_retry=18.0)
        prev = stats(rho=2.0, s_useful=1.0, concurrency=40.0, p_success=0.05, lambda_retry=20.0)
        self.assertTrue(labeling.check_conditions(now, prev, self.base, self.th)[3])


class LabelScenarioTests(unittest.TestCase):

    def make_records(self, storm_windows):
        records = []
        for w in range(12):
            t = w * 2.0
            if w in storm_windows:
                for k in range(10):
                    end = t + 0.1 + k * 0.18
                    records.append(call(end, 1900, 1, 0))
                    records.append(call(end + 0.005, 1900, 2, 0))
                    records.append(call(end + 0.01, 1900, 3, 0))
            else:
                for k in range(20):
                    records.append(call(t + 0.05 + k * 0.09, 80, 1, 200))
        return records

    def test_needs_tau_lock_windows_in_a_row(self):
        manifest = {"scenario_id": "x", "group": "t", "start": 0.0, "end": 24.0, "normal_sec": 8.0,
                    "phases": [{"name": "normal", "start": 0.0, "end": 8.0},
                               {"name": "fault", "start": 8.0, "end": 24.0}]}
        rows, baselines = labeling.label_scenario(manifest, self.make_records([6, 7, 8, 9, 10]), [],
                                                  labeling.DEFAULT_THRESHOLDS)
        gw = [r for r in rows if r["source_service"] == "gateway"]
        labels = [r["label"] for r in gw]
        # the first storm window (6) is not livelock yet: all four conditions
        # must hold for TAU_LOCK = 2 windows in a row. In the last storm
        # window (10) the calls in flight are already draining, so C3 is
        # false and the label switches off.
        self.assertEqual(labels[:7], ["normal"] * 7)
        self.assertEqual(labels[7:10], ["livelock"] * 3)
        self.assertEqual(labels[10:], ["normal", "normal"])

    def test_no_traffic_on_an_edge_is_skipped_not_crashed(self):
        manifest = {"scenario_id": "x", "group": "t", "start": 0.0, "end": 24.0, "normal_sec": 8.0,
                    "phases": [{"name": "normal", "start": 0.0, "end": 24.0}]}
        rows, baselines = labeling.label_scenario(manifest, self.make_records([]), [],
                                                  labeling.DEFAULT_THRESHOLDS)
        self.assertNotIn(PAY, baselines)
        self.assertTrue(all(r["label"] == "normal" for r in rows))


class ControllerTests(unittest.TestCase):

    def fresh_state(self):
        state = {}
        for edge in [GW, PAY, INV]:
            state[edge] = {"max_attempts": 3, "starting_max_attempts": 3, "backoff_base_ms": 100,
                           "backoff_multiplier": 1.0, "timeout_ms": 2000, "last_cut": 0.0,
                           "healthy_ticks": 0}
        return state

    def test_cut_on_storm_also_cuts_the_parent(self):
        state = self.fresh_state()
        now_stats = {GW: stats(rho=0.2), PAY: stats(rho=1.8, fail_ratio=0.7, overload_ratio=0.6),
                     INV: stats(rho=0.0)}
        probs, risky, healthy, actions, changed = controller.control_step(
            "reactive", None, state, now_stats, now_stats, 100.0)
        self.assertEqual(actions[PAY], "cut")
        self.assertEqual(actions[GW], "cut")      # path rule
        self.assertEqual(actions[INV], "hold")
        self.assertEqual(state[PAY]["max_attempts"], 2)
        self.assertEqual(state[PAY]["backoff_multiplier"], 2.0)
        self.assertTrue(changed)

    def test_cooldown_between_cuts(self):
        state = self.fresh_state()
        storm = {GW: stats(rho=2.0, overload_ratio=0.8), PAY: stats(), INV: stats()}
        controller.control_step("reactive", None, state, storm, storm, 100.0)
        controller.control_step("reactive", None, state, storm, storm, 100.5)
        self.assertEqual(state[GW]["max_attempts"], 2)
        controller.control_step("reactive", None, state, storm, storm, 102.5)
        self.assertEqual(state[GW]["max_attempts"], 1)
        controller.control_step("reactive", None, state, storm, storm, 105.0)
        self.assertEqual(state[GW]["max_attempts"], 1)   # never below 1

    def test_no_flapping_when_retries_are_off(self):
        # The Review-2 bug: at max_attempts=1, rho is 0 by definition. The
        # callee is still failing, so the controller must NOT give retries back.
        state = self.fresh_state()
        state[GW]["max_attempts"] = 1
        state[GW]["last_cut"] = 0.0
        still_broken = {GW: stats(rho=0.0, fail_ratio=0.9, overload_ratio=0.9), PAY: stats(), INV: stats()}
        for t in range(10, 30):
            controller.control_step("reactive", None, state, still_broken, still_broken, float(t))
        self.assertEqual(state[GW]["max_attempts"], 1)

    def test_restore_slowly_once_healthy(self):
        state = self.fresh_state()
        state[GW]["max_attempts"] = 1
        state[GW]["backoff_multiplier"] = 4.0
        state[GW]["last_cut"] = 100.0
        healthy = {GW: stats(rho=0.0, fail_ratio=0.02), PAY: stats(), INV: stats()}
        controller.control_step("reactive", None, state, healthy, healthy, 101.0)
        controller.control_step("reactive", None, state, healthy, healthy, 102.0)
        controller.control_step("reactive", None, state, healthy, healthy, 103.0)
        self.assertEqual(state[GW]["max_attempts"], 1)   # HOLD_SEC not over yet
        controller.control_step("reactive", None, state, healthy, healthy, 104.0)
        self.assertEqual(state[GW]["max_attempts"], 2)   # one step only
        controller.control_step("reactive", None, state, healthy, healthy, 105.0)
        self.assertEqual(state[GW]["max_attempts"], 2)   # needs 3 more healthy ticks

    def test_errors_with_spare_capacity_do_not_cut(self):
        # callee fails 70% of calls with 500 but has free slots: a retry is a
        # fresh chance to succeed, so the controller leaves retries alone
        state = self.fresh_state()
        errors = {GW: stats(), PAY: stats(rho=1.2, fail_ratio=0.7, overload_ratio=0.0), INV: stats()}
        probs, risky, healthy, actions, changed = controller.control_step(
            "reactive", None, state, errors, errors, 100.0)
        self.assertFalse(risky[PAY])
        self.assertFalse(changed)

    def test_retries_come_back_when_congestion_clears(self):
        state = self.fresh_state()
        state[PAY]["max_attempts"] = 1
        state[PAY]["last_cut"] = 100.0
        failing_not_busy = {GW: stats(), PAY: stats(rho=0.0, fail_ratio=0.7, overload_ratio=0.0), INV: stats()}
        for t in [101.0, 102.0, 103.0, 104.0]:
            controller.control_step("reactive", None, state, failing_not_busy, failing_not_busy, t)
        self.assertEqual(state[PAY]["max_attempts"], 2)

    def test_quiet_edge_is_never_judged(self):
        state = self.fresh_state()
        quiet = {GW: stats(n_calls=0, rho=0.0), PAY: stats(n_calls=1, rho=10.0), INV: stats(n_calls=0)}
        probs, risky, healthy, actions, changed = controller.control_step(
            "reactive", None, state, quiet, quiet, 50.0)
        self.assertFalse(any(risky.values()))
        self.assertFalse(changed)

    def test_predictive_mode_uses_the_model(self):
        model = {"feature_names": ["rho"], "mean": [0.0], "std": [1.0], "weights": [0.0],
                 "intercept": 10.0, "threshold": 0.5,
                 "baselines": {"gateway->order_service": {"latency_ms": 100, "concurrency": 2, "s_useful": 20},
                               "order_service->payment_service": {"latency_ms": 60, "concurrency": 1, "s_useful": 20},
                               "order_service->inventory_service": {"latency_ms": 60, "concurrency": 1, "s_useful": 20}}}
        # the model always says "high risk" (intercept 10)
        state = self.fresh_state()
        calm = {GW: stats(rho=0.0), PAY: stats(rho=0.0), INV: stats(rho=0.0)}
        probs, risky, healthy, actions, changed = controller.control_step(
            "predictive", model, state, calm, calm, 10.0)
        self.assertGreater(probs[GW], 0.99)
        self.assertFalse(risky[GW])        # but nothing is congested: no cut
        # payment's response time has more than doubled: congestion is starting
        slow = {GW: stats(rho=0.0), PAY: stats(rho=0.0, mean_latency_ms=150.0), INV: stats(rho=0.0)}
        probs, risky, healthy, actions, changed = controller.control_step(
            "predictive", model, state, slow, slow, 20.0)
        self.assertTrue(risky[PAY])
        self.assertTrue(risky[GW])         # rho is still 0 here: this is the pre-emptive cut
        self.assertEqual(actions[GW], "cut")

    def test_predictive_mode_ignores_fast_errors(self):
        # payment fails 70% of calls but answers fast and has free slots
        model = {"feature_names": ["fail_ratio"], "mean": [0.0], "std": [1.0], "weights": [20.0],
                 "intercept": 0.0, "threshold": 0.5,
                 "baselines": {"gateway->order_service": {"latency_ms": 150, "concurrency": 2, "s_useful": 20},
                               "order_service->payment_service": {"latency_ms": 60, "concurrency": 1, "s_useful": 20},
                               "order_service->inventory_service": {"latency_ms": 60, "concurrency": 1, "s_useful": 20}}}
        state = self.fresh_state()
        errors = {GW: stats(rho=0.3, mean_latency_ms=400.0),   # slower only because of back-offs
                  PAY: stats(rho=0.9, fail_ratio=0.7, mean_latency_ms=62.0), INV: stats()}
        probs, risky, healthy, actions, changed = controller.control_step(
            "predictive", model, state, errors, errors, 10.0)
        self.assertGreater(probs[PAY], 0.99)
        self.assertFalse(risky[PAY])
        self.assertFalse(risky[GW])


class PolicyFileTests(unittest.TestCase):

    def setUp(self):
        self.folder = tempfile.mkdtemp()
        self.old_path = retry_policy.POLICY_PATH
        self.old_dir = retry_policy.DATA_DIR
        retry_policy.POLICY_PATH = os.path.join(self.folder, "retry_policy.json")
        retry_policy.DATA_DIR = self.folder
        retry_policy.last_good_policy.clear()

    def tearDown(self):
        retry_policy.POLICY_PATH = self.old_path
        retry_policy.DATA_DIR = self.old_dir
        shutil.rmtree(self.folder)

    def test_no_file_means_default(self):
        self.assertEqual(retry_policy.get_retry_policy("gateway->order_service")["max_attempts"], 3)

    def test_round_trip(self):
        retry_policy.set_retry_policy("gateway->order_service", 2, 250, 1500, 0.1)
        p = retry_policy.get_retry_policy("gateway->order_service")
        self.assertEqual((p["max_attempts"], p["backoff_base_ms"], p["timeout_ms"], p["budget_ratio"]),
                         (2, 250, 1500, 0.1))
        self.assertEqual(retry_policy.get_retry_policy("unknown->edge")["max_attempts"], 3)

    def test_corrupt_file_falls_back(self):
        retry_policy.set_retry_policy("gateway->order_service", 1, 100)
        retry_policy.get_retry_policy("gateway->order_service")
        with open(retry_policy.POLICY_PATH, "w") as f:
            f.write('{"gateway->order_service": {"max_att')    # half-written file
        self.assertEqual(retry_policy.get_retry_policy("gateway->order_service")["max_attempts"], 1)

    def test_nonsense_values_are_cleaned(self):
        with open(retry_policy.POLICY_PATH, "w") as f:
            json.dump({"gateway->order_service": {"max_attempts": 0, "backoff_base_ms": -5,
                                                  "timeout_ms": "abc", "budget_ratio": "x"}}, f)
        p = retry_policy.get_retry_policy("gateway->order_service")
        self.assertEqual(p["max_attempts"], 1)
        self.assertEqual(p["backoff_base_ms"], 0)
        self.assertEqual(p["timeout_ms"], retry_policy.DEFAULT_TIMEOUT_MS)
        self.assertIsNone(p["budget_ratio"])

    def test_atomic_write_leaves_no_temp_files(self):
        path = os.path.join(self.folder, "x.json")
        for i in range(20):
            safe_files.write_json_atomic(path, {"i": i})
        self.assertEqual(safe_files.read_json(path), {"i": 19})
        self.assertEqual(os.listdir(self.folder), ["x.json"])
        with open(path, "w") as f:
            f.write("not json")
        self.assertIsNone(safe_files.read_json(path))
        self.assertIsNone(safe_files.read_json(os.path.join(self.folder, "missing.json")))


class FakeResponse(object):
    def __init__(self, code):
        self.status_code = code


class RetryClientTests(unittest.TestCase):

    def setUp(self):
        self.logged = []
        self.codes = []
        self.saved = (retry_client.requests.get, retry_client.log_call,
                      retry_client.get_retry_policy, retry_client.time.sleep)
        retry_client.requests.get = self.fake_get
        retry_client.log_call = self.fake_log
        retry_client.time.sleep = self.no_sleep
        retry_client.budget_tokens.clear()

    def tearDown(self):
        (retry_client.requests.get, retry_client.log_call,
         retry_client.get_retry_policy, retry_client.time.sleep) = self.saved

    def fake_get(self, url, params=None, headers=None, timeout=None):
        self.assertIn("X-Deadline", headers)
        code = self.codes.pop(0)
        if code == 0:
            raise retry_client.requests.exceptions.Timeout()
        return FakeResponse(code)

    def fake_log(self, **kwargs):
        self.logged.append(kwargs)

    def no_sleep(self, seconds):
        pass

    def use_policy(self, max_attempts, budget_ratio=None):
        policy = retry_policy.default_policy()
        policy["max_attempts"] = max_attempts
        policy["budget_ratio"] = budget_ratio

        def fixed_policy(name):
            return policy
        retry_client.get_retry_policy = fixed_policy

    def test_gives_up_after_max_attempts(self):
        self.use_policy(3)
        self.codes = [503, 0, 500]
        self.assertFalse(retry_client.call_with_retry("gateway", "order_service", "u", "s", "r"))
        self.assertEqual([r["attempt_number"] for r in self.logged], [1, 2, 3])
        self.assertEqual(len(set([r["call_id"] for r in self.logged])), 1)
        self.assertEqual(self.logged[1]["response_code"], 0)   # timeout recorded as 0
        self.assertEqual(self.logged[2]["backoff_delay_ms"], 0)

    def test_stops_on_success(self):
        self.use_policy(3)
        self.codes = [500, 200]
        self.assertTrue(retry_client.call_with_retry("gateway", "order_service", "u", "s", "r"))
        self.assertEqual(len(self.logged), 2)

    def test_budget_blocks_retries_when_empty(self):
        self.use_policy(3, budget_ratio=0.1)
        retry_client.budget_tokens["gateway->order_service"] = 0.0
        self.codes = [500]
        self.assertFalse(retry_client.call_with_retry("gateway", "order_service", "u", "s", "r"))
        self.assertEqual(len(self.logged), 1)
        self.assertTrue(self.logged[0]["budget_blocked"])

    def test_budget_refills_from_fresh_calls(self):
        name = "a->b"
        retry_client.budget_tokens[name] = 0.0
        for i in range(10):
            retry_client.budget_deposit(name, 0.1)
        self.assertTrue(retry_client.budget_take(name))
        self.assertFalse(retry_client.budget_take(name))

    def test_jitter_stays_in_range(self):
        for attempt in [1, 2, 3]:
            full = 100 * 2 ** (attempt - 1)
            for i in range(200):
                delay = retry_client.backoff_with_jitter_ms(100, attempt)
                self.assertTrue(full / 2.0 <= delay <= full)


class TailReaderTests(unittest.TestCase):

    def setUp(self):
        self.folder = tempfile.mkdtemp()
        self.old = telemetry.DATA_DIR
        telemetry.DATA_DIR = self.folder

    def tearDown(self):
        telemetry.DATA_DIR = self.old
        shutil.rmtree(self.folder)

    def test_partial_lines_and_reset(self):
        path = os.path.join(self.folder, "telemetry_gateway.jsonl")
        state = telemetry.make_tail_state(start_at_end=True)
        with open(path, "w") as f:
            f.write('{"a": 1}\n{"a": 2}\n{"a"')
        self.assertEqual(telemetry.read_new_call_records(state), [{"a": 1}, {"a": 2}])
        with open(path, "a") as f:
            f.write(': 3}\n')
        self.assertEqual(telemetry.read_new_call_records(state), [{"a": 3}])
        self.assertEqual(telemetry.read_new_call_records(state), [])
        with open(path, "w") as f:             # file reset by a new run
            f.write('{"a": 4}\n')
        self.assertEqual(telemetry.read_new_call_records(state), [{"a": 4}])

    def test_damaged_lines_are_skipped_and_counted(self):
        path = os.path.join(self.folder, "user_requests.jsonl")
        with open(path, "w") as f:
            f.write('{"ok": 1}\n\nbroken line\n{"ok": 2}\n')
        records, skipped = telemetry.read_jsonl(path)
        self.assertEqual(len(records), 2)
        self.assertEqual(skipped, 1)


class ScenarioTests(unittest.TestCase):

    def test_gradual_schedule(self):
        args = scenario_runner.parse_args(["--scenario_id", "x", "--mode", "gradual", "--ramp_sec", "10",
                                           "--ramp_steps", "5", "--hold_sec", "6", "--recovery_sec", "8"])
        schedule = scenario_runner.build_schedule(args)
        self.assertEqual([s[0] for s in schedule], ["normal"] + ["ramp"] * 5 + ["fault", "recovery"])
        self.assertAlmostEqual(schedule[1][2], 0.2)
        self.assertAlmostEqual(schedule[5][2], 1.0)

    def test_merge_phases(self):
        log = [("normal", 0, 10), ("ramp", 10, 12), ("ramp", 12, 14), ("fault", 14, 20), ("recovery", 20, 30)]
        phases = scenario_runner.merge_phases(log)
        self.assertEqual([(p["name"], p["start"], p["end"]) for p in phases],
                         [("normal", 0, 10), ("ramp", 10, 14), ("fault", 14, 20), ("recovery", 20, 30)])

    def test_review2_command_still_works(self):
        args = scenario_runner.parse_args(["--scenario_id", "demo1", "--mode", "gradual_degradation",
                                           "--normal_sec", "10", "--spike_sec", "20", "--ramp_steps", "5",
                                           "--hold_sec", "14", "--recovery_sec", "8", "--rps", "14"])
        self.assertEqual((args.mode, args.fault, args.ramp_sec, args.hold_sec), ("gradual", "latency", 20, 14))

    def test_bad_arguments_are_reported(self):
        args = scenario_runner.parse_args(["--scenario_id", "x", "--rps", "0", "--normal_sec", "1",
                                           "--peak_failure_rate", "1.5"])
        self.assertEqual(len(scenario_runner.check_args(args)), 3)

    def test_fault_severity(self):
        healthy = scenario_runner.fault_config("payment_service", "latency", 0.0, 800, 0.8)
        self.assertEqual((healthy["min_latency_ms"], healthy["max_latency_ms"]), (30, 90))
        full = scenario_runner.fault_config("payment_service", "latency", 1.0, 800, 0.8)
        self.assertEqual((full["min_latency_ms"], full["max_latency_ms"]), (700, 900))
        errors = scenario_runner.fault_config("payment_service", "error", 1.0, 800, 0.8)
        self.assertAlmostEqual(errors["failure_rate"], 0.8)


class MetricTests(unittest.TestCase):

    def test_recovery_time(self):
        users = []
        for i in range(30):
            ok = i >= 12      # everything fails for the first 12 seconds after the fault
            users.append({"start_time": 100.0 + i + 0.5, "succeeded": ok})
        seconds, recovered = experiment_arms.recovery_time(users, 100.0, 130.0, 1.0)
        self.assertTrue(recovered)
        self.assertEqual(seconds, 12.0)

    def test_never_recovers(self):
        users = [{"start_time": 100.0 + i, "succeeded": False} for i in range(20)]
        seconds, recovered = experiment_arms.recovery_time(users, 100.0, 120.0, 1.0)
        self.assertFalse(recovered)
        self.assertEqual(seconds, 20.0)

    def test_lead_time(self):
        examples = []
        for i in range(10):
            examples.append({"scenario_id": "s", "edge": GW, "window_index": i,
                             "label_now": 1 if i >= 6 else 0})
        alarms = [0, 0, 0, 1, 1, 1, 1, 1, 1, 1]
        leads, missed = prediction_model.lead_times(examples, alarms)
        self.assertEqual(leads, [6.0])    # alarm at window 3, livelock from window 6
        self.assertEqual(missed, 0)

    def test_risk_probability_never_overflows(self):
        model = {"feature_names": ["rho"], "mean": [0.0], "std": [0.0], "weights": [1000.0], "intercept": 0.0}
        self.assertEqual(features.risk_probability(model, {"rho": 10.0}), 1.0)
        self.assertEqual(features.risk_probability(model, {"rho": -10.0}), 0.0)


class EvaluationTests(unittest.TestCase):

    def test_confusion_matrix_metrics_by_hand(self):
        y = [1, 1, 0, 0, 1, 0]
        pred = [1, 0, 0, 1, 1, 0]          # TP 2, FN 1, TN 2, FP 1
        m = evaluate_model.threshold_metrics(y, pred)
        self.assertEqual((m["tp"], m["fp"], m["tn"], m["fn"]), (2, 1, 2, 1))
        self.assertAlmostEqual(m["accuracy"], 4 / 6.0)
        self.assertAlmostEqual(m["specificity"], 2 / 3.0)
        self.assertAlmostEqual(m["mcc"], (2 * 2 - 1 * 1) / 9.0)     # (TP*TN - FP*FN) / sqrt(3*3*3*3)
        self.assertAlmostEqual(m["kappa"], 1 / 3.0)

    def test_metrics_do_not_crash_when_one_class_is_predicted(self):
        m = evaluate_model.threshold_metrics([1, 0, 0, 1], [0, 0, 0, 0])
        self.assertEqual(m["mcc"], 0.0)
        self.assertEqual(m["precision"], 0.0)
        self.assertEqual(m["recall"], 0.0)

    def test_calibration_error(self):
        # always saying 0.5 when half really are positive: perfectly calibrated
        good = evaluate_model.calibration([1, 0, 1, 0], [0.5, 0.5, 0.5, 0.5])
        self.assertAlmostEqual(good["ece"], 0.0)
        self.assertAlmostEqual(good["brier"], 0.25)
        # always saying 0.9 when half are positive: off by 0.4
        bad = evaluate_model.calibration([1, 0, 1, 0], [0.9, 0.9, 0.9, 0.9])
        self.assertAlmostEqual(bad["ece"], 0.4)

    def test_bootstrap_resamples_whole_scenarios(self):
        rows = []
        for scenario, label in [("a", 1), ("b", 0), ("c", 1), ("d", 0)]:
            for k in range(3):
                rows.append({"scenario_id": scenario, "y": label, "risk": 0.9 if label else 0.1})
        low, high, used = evaluate_model.bootstrap(rows, evaluate_model.metric_on("risk", "f1"), rounds=50)
        self.assertEqual((low, high), (1.0, 1.0))      # perfect scores stay perfect in every resample
        self.assertTrue(0 < used <= 50)                 # resamples with only one class are skipped
        picked = evaluate_model.resample_scenarios(evaluate_model.random.Random(1), ["a", "b", "c"])
        self.assertEqual(len(picked), 3)
        for scenario in picked:
            self.assertIn(scenario, ["a", "b", "c"])


class CalibrationTests(unittest.TestCase):

    def test_reads_headerless_tar_gz(self):
        folder = tempfile.mkdtemp()
        try:
            rows = []
            for i in range(50):
                rows.append("%d,T%d,%d,0.1.%d,svcA,rpc,svcB,ifc,%d" % (i, i, i * 10, i % 3, 5 + i))
                rows.append("%d,T%d,%d,0.1.%d.1,svcB,rpc,svcC,ifc,-%d" % (i, i, i * 10, i % 3, 3 + i))
                rows.append("%d,T%d,%d,0.2,svcA,db,mysql,ifc,2" % (i, i, i * 10))
            data = ("\n".join(rows) + "\n").encode("utf-8")
            path = os.path.join(folder, "MSCallGraph_0.tar.gz")
            with tarfile.open(path, "w:gz") as archive:
                info = tarfile.TarInfo("MSCallGraph_0.csv")
                info.size = len(data)
                archive.addfile(info, io.BytesIO(data))
            downstreams, depths, rts = calibrate_from_trace.read_trace(path, 1000)
            self.assertEqual(sorted(downstreams.keys()), ["svcA", "svcB"])   # db call filtered out
            self.assertEqual(len(rts), 100)
            self.assertTrue(min(rts) > 0)     # negative (callee-side) values made positive
        finally:
            shutil.rmtree(folder)


if __name__ == "__main__":
    unittest.main()
