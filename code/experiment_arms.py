import argparse
import csv
import math
import os
import random
import subprocess
import sys
import time

import requests

from common.config import CODE_DIR, RESULTS_DIR, GATEWAY_URL, make_dirs
from common.retry_policy import write_uniform_policy, reset_policy
from common.telemetry import (read_call_records, read_user_records, read_server_records,
                              read_scenarios, within_run)
from run_all import wait_until_idle
import scenario_runner

# The headline experiment: the SAME fault, hit with five different retry
# strategies, each repeated several times in a shuffled order.
#
#   no_retry      max_attempts = 1 everywhere
#   static_retry  max_attempts = 3 everywhere (common industry default)
#   retry_budget  3 attempts, but retries capped at 10% of fresh calls per
#                 edge (Google SRE book / Finagle / gRPC retry throttling)
#   reactive      our controller, cuts when rho > 1
#   predictive    our controller, cuts when the model predicts livelock
#
# Two fault situations:
#   metastable    order_service has a long queue (10s). Payment slows down
#                 gradually, then recovers. Does the system recover too?
#   error_burst   short queue (1s). Payment starts failing 70% of calls
#                 (fast errors, not slowness). Here retries are USEFUL,
#                 because each attempt has a fresh chance to succeed.

EXPERIMENTS = {
    "metastable": {
        "mode": "gradual", "fault": "latency", "target": "payment_service",
        "queue_wait": 10.0, "rps": 20.0, "normal_sec": 10.0, "ramp_sec": 10.0,
        "ramp_steps": 5, "hold_sec": 10.0, "recovery_sec": 25.0,
        "peak_latency_ms": 800.0, "peak_failure_rate": 0.8,
    },
    "error_burst": {
        "mode": "gradual", "fault": "error", "target": "payment_service",
        "queue_wait": 1.0, "rps": 20.0, "normal_sec": 10.0, "ramp_sec": 10.0,
        "ramp_steps": 5, "hold_sec": 10.0, "recovery_sec": 15.0,
        "peak_latency_ms": 800.0, "peak_failure_rate": 0.7,
    },
}

ARMS = ["no_retry", "static_retry", "retry_budget", "reactive", "predictive"]
BUDGET_RATIO = 0.1
GROUP = "experiment"
RECOVERY_FRACTION = 0.9
RECOVERY_HOLD_SEC = 5

RUNS_PATH = os.path.join(RESULTS_DIR, "experiment_runs.csv")
SUMMARY_PATH = os.path.join(RESULTS_DIR, "experiment_summary.txt")


def scenario_argv(experiment, scenario_id, seed):
    settings = EXPERIMENTS[experiment]
    argv = ["--scenario_id", scenario_id, "--group", GROUP, "--quiet", "--seed", str(seed)]
    for key in ["mode", "fault", "target"]:
        argv.append("--" + key)
        argv.append(settings[key])
    for key in ["queue_wait", "rps", "normal_sec", "ramp_sec", "ramp_steps", "hold_sec",
                "recovery_sec", "peak_latency_ms", "peak_failure_rate"]:
        argv.append("--" + key)
        argv.append(str(settings[key]))
    return argv


def scenario_seconds(experiment):
    settings = EXPERIMENTS[experiment]
    total = settings["normal_sec"] + settings["hold_sec"] + settings["recovery_sec"]
    if settings["mode"] == "gradual":
        total = total + settings["ramp_sec"]
    return total


def run_one(experiment, arm, rep, seed):
    scenario_id = "exp_%s_%s_r%d" % (experiment, arm, rep)

    if arm == "no_retry":
        write_uniform_policy(max_attempts=1)
    elif arm == "retry_budget":
        write_uniform_policy(max_attempts=3, budget_ratio=BUDGET_RATIO)
    else:
        write_uniform_policy(max_attempts=3)

    controller = None
    if arm in ("reactive", "predictive"):
        command = [sys.executable, os.path.join(CODE_DIR, "controller.py"),
                   "--mode", arm, "--duration_sec", str(scenario_seconds(experiment) + 4),
                   "--tag", scenario_id, "--quiet"]
        controller = subprocess.Popen(command, cwd=CODE_DIR)
        time.sleep(1.5)
        if controller.poll() is not None:
            print("   ERROR: the controller exited straight away (code %s)" % controller.returncode)
            reset_policy()
            return None

    try:
        args = scenario_runner.parse_args(scenario_argv(experiment, scenario_id, seed))
        scenario_runner.run_scenario(args)
    finally:
        if controller is not None:
            try:
                controller.wait(timeout=15)
            except subprocess.TimeoutExpired:
                controller.terminate()
                controller.wait(timeout=5)
        # whatever happened, the next run starts from the default policy
        reset_policy()

    if not wait_until_idle(timeout_sec=60):
        print("   warning: services did not go idle within 60s")
    return scenario_id


# ---------------- metrics from the logs ----------------

def percentile(values, fraction):
    if len(values) == 0:
        return None
    ordered = sorted(values)
    index = int(round(fraction * (len(ordered) - 1)))
    return ordered[index]


def period_bounds(manifest):
    normal_end = None
    fault_end = None
    for phase in manifest["phases"]:
        if phase["name"] == "normal":
            normal_end = phase["end"]
        if phase["name"] in ("ramp", "fault"):
            fault_end = phase["end"]
    return {
        "normal": (manifest["start"], normal_end),
        "fault": (normal_end, fault_end),
        "recovery": (fault_end, manifest["end"]),
    }


def in_period(t, bounds):
    return bounds[0] <= t < bounds[1]


def recovery_time(user_records, fault_end, end, normal_rate):
    # seconds after the fault was removed until users get >= 90% of their
    # normal success rate, and keep getting it for RECOVERY_HOLD_SEC
    n_seconds = int(math.ceil(end - fault_end))
    buckets = []
    for i in range(n_seconds):
        buckets.append([0, 0])
    for record in user_records:
        index = int(math.floor(record["start_time"] - fault_end))
        if 0 <= index < n_seconds:
            buckets[index][0] = buckets[index][0] + 1
            if record["succeeded"]:
                buckets[index][1] = buckets[index][1] + 1

    target = RECOVERY_FRACTION * normal_rate
    for i in range(n_seconds - RECOVERY_HOLD_SEC + 1):
        total = 0
        ok = 0
        for k in range(i, i + RECOVERY_HOLD_SEC):
            total = total + buckets[k][0]
            ok = ok + buckets[k][1]
        if total > 0 and float(ok) / total >= target:
            return float(i), True
    return float(n_seconds), False


def run_metrics(manifest, users, calls, servers):
    bounds = period_bounds(manifest)
    metrics = {}

    for period in ["normal", "fault", "recovery"]:
        b = bounds[period]
        n = 0
        ok = 0
        latencies = []
        for record in users:
            if in_period(record["start_time"], b):
                n = n + 1
                if record["succeeded"]:
                    ok = ok + 1
                latencies.append(record["total_time_ms"])
        n_calls = 0
        n_retries = 0
        for record in calls:
            if in_period(record.get("start_time", record["timestamp"]), b):
                n_calls = n_calls + 1
                if record["attempt_number"] > 1:
                    n_retries = n_retries + 1
        done = 0
        orphaned = 0
        for record in servers:
            # orphaned work = order_service finishing a request after the
            # gateway already gave up on it (the queue is at order_service)
            if record["service"] != "order_service" or record["status"] == 503:
                continue
            if in_period(record["slot_time"], b):
                done = done + 1
                if record["orphaned"]:
                    orphaned = orphaned + 1

        seconds = b[1] - b[0]
        metrics[period + "_users"] = n
        metrics[period + "_success_pct"] = 100.0 * ok / n if n > 0 else 0.0
        metrics[period + "_goodput"] = ok / seconds if seconds > 0 else 0.0
        metrics[period + "_mean_latency_ms"] = sum(latencies) / len(latencies) if latencies else 0.0
        metrics[period + "_p95_latency_ms"] = percentile(latencies, 0.95) or 0.0
        metrics[period + "_calls"] = n_calls
        metrics[period + "_retries"] = n_retries
        # work per useful result. With zero successes the division is
        # undefined, so the count of successes is floored at 1
        metrics[period + "_calls_per_success"] = float(n_calls) / ok if ok > 0 else float(n_calls)
        metrics[period + "_orphaned_pct"] = 100.0 * orphaned / done if done > 0 else 0.0

    normal_rate = metrics["normal_success_pct"] / 100.0
    seconds, recovered = recovery_time(users, bounds["recovery"][0], bounds["recovery"][1], normal_rate)
    metrics["recovery_sec"] = seconds
    metrics["recovered"] = recovered
    return metrics


def collect_results():
    manifests = []
    for manifest in read_scenarios():
        if manifest.get("group") == GROUP and not manifest.get("interrupted"):
            manifests.append(manifest)
    ids = [m["scenario_id"] for m in manifests]
    users, s1 = read_user_records(ids)
    calls, s2 = read_call_records(ids)
    servers, s3 = read_server_records(ids)

    by_id = {}
    for scenario_id in ids:
        by_id[scenario_id] = {"users": [], "calls": [], "servers": []}
    for record in users:
        by_id[record["scenario_id"]]["users"].append(record)
    for record in calls:
        by_id[record["scenario_id"]]["calls"].append(record)
    for record in servers:
        by_id[record["scenario_id"]]["servers"].append(record)

    rows = []
    for manifest in manifests:
        scenario_id = manifest["scenario_id"]
        parts = scenario_id.split("_")
        # exp_<experiment>_<arm>_r<rep>; experiment and arm names contain "_"
        rep = int(parts[-1][1:])
        experiment = None
        arm = None
        for name in EXPERIMENTS:
            if scenario_id.startswith("exp_" + name + "_"):
                experiment = name
                arm = scenario_id[len("exp_" + name + "_"):-(len(parts[-1]) + 1)]
        if experiment is None or arm not in ARMS:
            continue
        data = by_id[scenario_id]
        metrics = run_metrics(manifest, within_run(data["users"], manifest),
                              within_run(data["calls"], manifest),
                              within_run(data["servers"], manifest))
        row = {"experiment": experiment, "arm": arm, "rep": rep, "scenario_id": scenario_id}
        row.update(metrics)
        rows.append(row)
    return rows


def mean_std(values):
    if len(values) == 0:
        return 0.0, 0.0
    mean = sum(values) / len(values)
    if len(values) < 2:
        return mean, 0.0
    var = 0.0
    for v in values:
        var = var + (v - mean) ** 2
    return mean, (var / (len(values) - 1)) ** 0.5


def welch_p(a, b):
    if len(a) < 2 or len(b) < 2:
        return None
    try:
        from scipy.stats import ttest_ind
        result = ttest_ind(a, b, equal_var=False)
        if math.isnan(result.pvalue):
            return None
        return float(result.pvalue)
    except ImportError:
        return None


SUMMARY_COLUMNS = [
    ("normal_success_pct", "success% normal", "%.1f"),
    ("fault_success_pct", "success% fault", "%.1f"),
    ("recovery_success_pct", "success% after", "%.1f"),
    ("recovery_sec", "recovery s", "%.1f"),
    ("fault_calls", "calls in fault", "%.0f"),
    ("fault_calls_per_success", "calls/success", "%.1f"),
    ("fault_orphaned_pct", "orphaned% fault", "%.1f"),
    ("recovery_orphaned_pct", "orphaned% after", "%.1f"),
    ("fault_p95_latency_ms", "p95 ms fault", "%.0f"),
]


def write_summary(rows):
    fieldnames = list(rows[0].keys())
    with open(RUNS_PATH, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow(row)

    lines = []
    lines.append("Retry strategies under the same fault (mean +/- std over repeated runs)")
    lines.append("=" * 72)
    for experiment in EXPERIMENTS:
        exp_rows = [r for r in rows if r["experiment"] == experiment]
        if len(exp_rows) == 0:
            continue
        lines.append("")
        lines.append("EXPERIMENT: %s   %s" % (experiment, describe(experiment)))
        for key, title, form in SUMMARY_COLUMNS:
            lines.append("  %s" % title)
            for arm in ARMS:
                values = [r[key] for r in exp_rows if r["arm"] == arm]
                if len(values) == 0:
                    continue
                mean, std = mean_std(values)
                each = ", ".join([form % v for v in values])
                lines.append("    %-14s %10s +/- %-8s (n=%d: %s)" % (arm, form % mean, form % std, len(values), each))
        recovered_line = "  recovered within the observation window: "
        parts = []
        for arm in ARMS:
            arm_rows = [r for r in exp_rows if r["arm"] == arm]
            if len(arm_rows) > 0:
                n_ok = len([r for r in arm_rows if r["recovered"]])
                parts.append("%s %d/%d" % (arm, n_ok, len(arm_rows)))
        lines.append(recovered_line + ", ".join(parts))

        lines.append("  significance (Welch t-test p-values, n is small so read with care):")
        for key in ["recovery_success_pct", "fault_success_pct", "fault_calls", "recovery_sec"]:
            for a, b in [("static_retry", "reactive"), ("static_retry", "predictive"),
                         ("retry_budget", "predictive"), ("reactive", "predictive")]:
                va = [r[key] for r in exp_rows if r["arm"] == a]
                vb = [r[key] for r in exp_rows if r["arm"] == b]
                p = welch_p(va, vb)
                if p is not None:
                    lines.append("    %-22s %-13s vs %-11s p = %.3f" % (key, a, b, p))

    text = "\n".join(lines)
    with open(SUMMARY_PATH, "w") as f:
        f.write(text + "\n")
    return text


def describe(experiment):
    s = EXPERIMENTS[experiment]
    if s["fault"] == "latency":
        what = "payment latency ramps to ~%dms" % s["peak_latency_ms"]
    else:
        what = "payment error rate ramps to %.0f%%" % (100 * s["peak_failure_rate"])
    return "(%s, order queue %.0fs, %.0f users/s)" % (what, s["queue_wait"], s["rps"])


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--reps", type=int, default=3)
    parser.add_argument("--experiments", type=str, default="metastable,error_burst")
    parser.add_argument("--arms", type=str, default=",".join(ARMS))
    parser.add_argument("--seed", type=int, default=11)
    parser.add_argument("--summary_only", action="store_true",
                        help="do not run anything, just recompute the tables from the logs")
    args = parser.parse_args()
    make_dirs()

    experiments = [e for e in args.experiments.split(",") if e != ""]
    arms = [a for a in args.arms.split(",") if a != ""]
    for e in experiments:
        if e not in EXPERIMENTS:
            print("unknown experiment '%s'. choose from %s" % (e, list(EXPERIMENTS.keys())))
            sys.exit(2)
    for a in arms:
        if a not in ARMS:
            print("unknown arm '%s'. choose from %s" % (a, ARMS))
            sys.exit(2)

    if not args.summary_only:
        try:
            requests.get(GATEWAY_URL.replace("/checkout", "/health"), timeout=2)
        except requests.exceptions.RequestException:
            print("error: the services are not running. Start them first:  python run_all.py")
            sys.exit(1)
        if "predictive" in arms and not os.path.exists(os.path.join(RESULTS_DIR, "model.json")):
            print("error: the predictive arm needs results/model.json. Run prediction_model.py first,")
            print("or leave it out:  --arms no_retry,static_retry,retry_budget,reactive")
            sys.exit(1)

        done_ids = set()
        for manifest in read_scenarios():
            if manifest.get("group") == GROUP and not manifest.get("interrupted"):
                done_ids.add(manifest["scenario_id"])

        rng = random.Random(args.seed)
        plan = []
        for rep in range(1, args.reps + 1):
            for experiment in experiments:
                order = list(arms)
                rng.shuffle(order)
                for arm in order:
                    plan.append((experiment, arm, rep, rng.randint(1, 100000)))

        total = 0.0
        for experiment, arm, rep, seed in plan:
            total = total + scenario_seconds(experiment) + 10
        print("%d runs planned, roughly %.0f minutes" % (len(plan), total / 60.0))

        n = 0
        for experiment, arm, rep, seed in plan:
            n = n + 1
            scenario_id = "exp_%s_%s_r%d" % (experiment, arm, rep)
            if scenario_id in done_ids:
                print("[%d/%d] %s already done, skipping" % (n, len(plan), scenario_id))
                continue
            print("[%d/%d] %s" % (n, len(plan), scenario_id))
            try:
                run_one(experiment, arm, rep, seed)
            except KeyboardInterrupt:
                print("\nstopped. Finished runs are kept; run this again to continue where it left off.")
                sys.exit(130)

    rows = collect_results()
    if len(rows) == 0:
        print("no experiment runs found in the logs.")
        sys.exit(1)
    print("")
    print(write_summary(rows))


if __name__ == "__main__":
    main()
