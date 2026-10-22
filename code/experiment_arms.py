import argparse
import csv
import math
import os
import random
import subprocess
import sys
import time
import warnings

import requests

from common.config import CODE_DIR, RESULTS_DIR, GATEWAY_URL, make_dirs
from common.retry_policy import write_uniform_policy, reset_policy
from common.telemetry import (read_call_records, read_user_records, read_server_records,
                              read_scenarios, within_run)
from run_all import wait_until_idle
import scenario_runner
from common.plots import plt, save_figure, BLUE, ORANGE, GREY, LIGHT_GREY


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
        reset_policy()

    if not wait_until_idle(timeout_sec=60):
        print("   warning: services did not go idle within 60s")
    return scenario_id


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
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            result = ttest_ind(a, b, equal_var=False)
        if math.isnan(result.pvalue):
            return None
        return float(result.pvalue)
    except ImportError:
        return None


ARM_LABELS = {"no_retry": "No retry", "static_retry": "Static retry", "retry_budget": "Retry budget 10%",
              "reactive": "Reactive (ours)", "predictive": "Predictive (ours)"}

SUMMARY_COLUMNS = [
    ("normal_success_pct", "Healthy%", "%.1f"),
    ("fault_success_pct", "Fault%", "%.1f"),
    ("recovery_success_pct", "After%", "%.1f"),
    ("recovery_sec", "Recov(s)", "%.1f"),
    ("fault_calls", "Calls", "%.0f"),
    ("recovery_orphaned_pct", "Orphan%", "%.1f"),
]

SIGNIFICANCE = [
    ("metastable", "recovery_success_pct", "static_retry"),
    ("metastable", "recovery_success_pct", "retry_budget"),
    ("metastable", "recovery_success_pct", "reactive"),
    ("metastable", "fault_calls", "static_retry"),
    ("error_burst", "fault_success_pct", "static_retry"),
    ("error_burst", "fault_success_pct", "retry_budget"),
]


def cell(values, form):
    mean, std = mean_std(values)
    return (form % mean) + "+-" + (form % std)


def write_summary(rows):
    fieldnames = list(rows[0].keys())
    with open(RUNS_PATH, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow(row)

    lines = []
    n_reps = max([r["rep"] for r in rows])
    lines.append("RETRY STRATEGIES  |  %d strategies x %d runs per fault  |  mean+-std" % (len(ARMS), n_reps))
    table_number = 1
    for experiment in EXPERIMENTS:
        exp_rows = [r for r in rows if r["experiment"] == experiment]
        if len(exp_rows) == 0:
            continue
        lines.append("")
        lines.append("Table %d. %s %s" % (table_number, experiment, describe(experiment)))
        table_number = table_number + 1
        header = "%-18s %10s" % ("Strategy", "Recovered")
        for key, title, form in SUMMARY_COLUMNS:
            header = header + " %11s" % title
        lines.append(header)
        lines.append("-" * len(header))
        for arm in ARMS:
            arm_rows = [r for r in exp_rows if r["arm"] == arm]
            if len(arm_rows) == 0:
                continue
            n_ok = len([r for r in arm_rows if r["recovered"]])
            line = "%-18s %10s" % (ARM_LABELS[arm], "%d/%d" % (n_ok, len(arm_rows)))
            for key, title, form in SUMMARY_COLUMNS:
                line = line + " %11s" % cell([r[key] for r in arm_rows], form)
            lines.append(line)

    lines.append("")
    lines.append("Table %d. Welch t-test, predictive vs other strategies" % table_number)
    header = "%-12s %-22s %-18s %8s %8s %8s" % ("Experiment", "Metric", "Other strategy", "Other", "Pred.", "p")
    lines.append(header)
    lines.append("-" * len(header))
    for experiment, key, other in SIGNIFICANCE:
        exp_rows = [r for r in rows if r["experiment"] == experiment]
        va = [r[key] for r in exp_rows if r["arm"] == other]
        vb = [r[key] for r in exp_rows if r["arm"] == "predictive"]
        p = welch_p(va, vb)
        if p is None:
            continue
        lines.append("%-12s %-22s %-18s %8.1f %8.1f %8.3f" % (experiment, key, ARM_LABELS[other],
                                                            mean_std(va)[0], mean_std(vb)[0], p))

    text = "\n".join(lines)
    with open(SUMMARY_PATH, "w") as f:
        f.write(text + "\n")
    return text


def draw_summary(rows, show):
    experiments = [e for e in EXPERIMENTS if len([r for r in rows if r["experiment"] == e]) > 0]
    fig, axes = plt.subplots(2, len(experiments), figsize=(6.2 * len(experiments), 7.6), squeeze=False)
    periods = [("normal_success_pct", "Healthy", LIGHT_GREY), ("fault_success_pct", "During fault", ORANGE),
               ("recovery_success_pct", "After fault", BLUE)]
    labels = [ARM_LABELS[a].replace(" (ours)", "\n(ours)").replace(" 10%", "\n10%") for a in ARMS]
    for col in range(len(experiments)):
        experiment = experiments[col]
        exp_rows = [r for r in rows if r["experiment"] == experiment]
        ax = axes[0][col]
        width = 0.26
        for k in range(len(periods)):
            key, label, colour = periods[k]
            means = []
            stds = []
            for arm in ARMS:
                mean, std = mean_std([r[key] for r in exp_rows if r["arm"] == arm])
                means.append(mean)
                stds.append(std)
            xs = [i + (k - 1) * width for i in range(len(ARMS))]
            ax.bar(xs, means, width=width, color=colour, yerr=stds, capsize=2, label=label)
            ax.grid(False, axis="x")
        ax.set_xticks(range(len(ARMS)))
        ax.set_xticklabels(labels, fontsize=8)
        ax.set_ylim(0, 112)
        ax.set_ylabel("Users served (%)")
        ax.grid(axis="x", visible=False)
        ax.set_title("(%s) %s: users served" % ("ab"[col], experiment.replace("_", " ")))
        ax.legend(loc="upper center", ncol=3, fontsize=8)

        ax = axes[1][col]
        means = []
        stds = []
        for arm in ARMS:
            mean, std = mean_std([r["fault_calls"] for r in exp_rows if r["arm"] == arm])
            means.append(mean)
            stds.append(std)
        colours = [BLUE if a in ("reactive", "predictive") else GREY for a in ARMS]
        ax.bar(range(len(ARMS)), means, yerr=stds, capsize=3, color=colours, width=0.55)
        ax.grid(False, axis="x")
        for i in range(len(ARMS)):
            arm_rows = [r for r in exp_rows if r["arm"] == ARMS[i]]
            n_ok = len([r for r in arm_rows if r["recovered"]])
            ax.text(i, means[i] + stds[i] + max(means) * 0.03, "recovered\n%d/%d" % (n_ok, len(arm_rows)),
                    ha="center", fontsize=8)
        ax.set_xticks(range(len(ARMS)))
        ax.set_xticklabels(labels, fontsize=8)
        ax.set_ylim(0, max(means) * 1.3)
        ax.set_ylabel("Internal calls during fault")
        ax.grid(axis="x", visible=False)
        ax.set_title("(%s) %s: work done during the fault" % ("cd"[col], experiment.replace("_", " ")))
    fig.tight_layout()
    return save_figure(fig, "experiments.png", show)


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
    parser.add_argument("--summary_only", action="store_true")
    parser.add_argument("--no_show", action="store_true")
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
        print("error: no experiment runs in the logs")
        sys.exit(1)
    if not args.summary_only:
        print("")
    print(write_summary(rows))
    print("")
    draw_summary(rows, not args.no_show)


if __name__ == "__main__":
    main()
