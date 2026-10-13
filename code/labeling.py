import argparse
import csv
import json
import math
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from common.config import DATA_DIR, RESULTS_DIR, EDGES, edge_name, make_dirs
from common.telemetry import read_call_records, read_user_records, read_scenarios, within_run
from features import window_stats_for_edge, split_by_edge, WINDOW_SEC

# Module 4: livelock detection. Applies the four formal conditions to every
# 2-second window of every edge of every scenario, and writes the result
# (one row per edge per window) to data/labeled_windows.csv.
#
# A window is LIVELOCK when all four hold, for TAU_LOCK windows in a row:
#   C1 retry amplification is high      rho > RHO_CRIT
#   C2 useful throughput has collapsed  s_useful < BETA x healthy s_useful
#   C3 work in flight is not draining   concurrency > C_FACTOR x healthy,
#                                       and not falling compared to 4s ago
#   C4 the retry loop is not damping    retries not falling AND success
#                                       not improving, compared to 4s ago
#
# "healthy" = this edge's own level during the first WARMUP_SEC of the same
# scenario (every scenario starts with normal traffic), so the conditions
# are relative and work for edges with very different traffic levels.
# The trend tests allow TREND_TOL of noise: a flat, stuck storm counts as
# "not falling"; only a clear improvement counts as recovering.

WARMUP_SEC = 8.0
MIN_CALLS = 4

RHO_CRIT = 1.0
BETA = 0.5
C_FACTOR = 2.0
TREND_LOOKBACK = 2
TREND_TOL = 0.2
P_TOL = 0.05
TAU_LOCK = 2

DEFAULT_THRESHOLDS = {
    "RHO_CRIT": RHO_CRIT,
    "BETA": BETA,
    "C_FACTOR": C_FACTOR,
    "TREND_TOL": TREND_TOL,
    "P_TOL": P_TOL,
    "TAU_LOCK": TAU_LOCK,
}

FIELDNAMES = [
    "scenario_id", "group", "source_service", "dest_service",
    "window_index", "window_start", "t_rel", "phase",
    "n_calls", "lambda_base", "lambda_retry", "rho", "s_useful", "concurrency",
    "p_success", "fail_ratio", "overload_ratio", "mean_latency_ms",
    "user_requests", "user_success_rate",
    "cond1_rho_high", "cond2_throughput_collapse", "cond3_concurrency_stuck",
    "cond4_positive_feedback", "label",
]


def median_of(values):
    if len(values) == 0:
        return None
    sorted_values = sorted(values)
    middle = len(sorted_values) // 2
    if len(sorted_values) % 2 == 1:
        return sorted_values[middle]
    return (sorted_values[middle - 1] + sorted_values[middle]) / 2.0


def phase_at(manifest, t):
    for phase in manifest["phases"]:
        if phase["start"] <= t < phase["end"]:
            return phase["name"]
    return "after_end"


def edge_baseline(stats_list, manifest):
    # median over the warm-up windows (the first seconds of normal traffic)
    warmup_sec = min(WARMUP_SEC, manifest["normal_sec"])
    n_warmup = int(warmup_sec // WINDOW_SEC)

    s_values = []
    c_values = []
    l_values = []
    for i in range(min(n_warmup, len(stats_list))):
        stats = stats_list[i]
        if stats["n_calls"] < MIN_CALLS:
            continue
        s_values.append(stats["s_useful"])
        c_values.append(stats["concurrency"])
        l_values.append(stats["mean_latency_ms"])

    if len(s_values) == 0:
        return None
    return {
        "s_useful": median_of(s_values),
        "concurrency": median_of(c_values),
        "latency_ms": median_of(l_values),
    }


def check_conditions(curr, trend, baseline, th):
    if curr["n_calls"] < MIN_CALLS:
        return False, False, False, False

    cond1 = curr["rho"] > th["RHO_CRIT"]

    cond2 = curr["s_useful"] < th["BETA"] * baseline["s_useful"]

    concurrency_floor = max(th["C_FACTOR"] * baseline["concurrency"], 1.0)
    concurrency_high = curr["concurrency"] > concurrency_floor
    not_draining = True
    if trend is not None:
        not_draining = curr["concurrency"] >= (1.0 - th["TREND_TOL"]) * trend["concurrency"]
    cond3 = concurrency_high and not_draining

    cond4 = False
    if trend is not None and trend["p_success"] is not None and curr["p_success"] is not None:
        retries_not_falling = curr["lambda_retry"] >= (1.0 - th["TREND_TOL"]) * trend["lambda_retry"]
        success_not_improving = curr["p_success"] <= trend["p_success"] + th["P_TOL"]
        cond4 = retries_not_falling and success_not_improving and curr["lambda_retry"] > 0

    return cond1, cond2, cond3, cond4


def user_windows(user_records, t0, n_windows):
    # per window: how many users ARRIVED, and what fraction of them got a success
    counts = []
    for i in range(n_windows):
        counts.append([0, 0])
    for record in user_records:
        index = int(math.floor((record["start_time"] - t0) / WINDOW_SEC))
        if 0 <= index < n_windows:
            counts[index][0] = counts[index][0] + 1
            if record["succeeded"]:
                counts[index][1] = counts[index][1] + 1
    return counts


def label_scenario(manifest, call_records, user_records, th):
    t0 = manifest["start"]
    n_windows = int(math.ceil((manifest["end"] - t0) / WINDOW_SEC))
    if n_windows <= 0:
        return [], {}

    by_edge = split_by_edge(call_records)
    users = user_windows(user_records, t0, n_windows)

    rows = []
    baselines = {}
    for edge in EDGES:
        stats_list = window_stats_for_edge(by_edge[edge], t0, n_windows)
        baseline = edge_baseline(stats_list, manifest)
        if baseline is None:
            # no healthy traffic on this edge at the start: cannot judge it
            continue
        baselines[edge] = baseline

        consecutive = 0
        for i in range(n_windows):
            stats = stats_list[i]
            trend = None
            if i >= TREND_LOOKBACK:
                trend = stats_list[i - TREND_LOOKBACK]

            c1, c2, c3, c4 = check_conditions(stats, trend, baseline, th)
            if c1 and c2 and c3 and c4:
                consecutive = consecutive + 1
            else:
                consecutive = 0
            label = "livelock" if consecutive >= th["TAU_LOCK"] else "normal"

            window_start = t0 + i * WINDOW_SEC
            n_users, n_users_ok = users[i]
            user_success_rate = ""
            if n_users > 0:
                user_success_rate = round(float(n_users_ok) / n_users, 4)

            rows.append({
                "scenario_id": manifest["scenario_id"],
                "group": manifest.get("group", ""),
                "source_service": edge[0],
                "dest_service": edge[1],
                "window_index": i,
                "window_start": round(window_start, 3),
                "t_rel": round(i * WINDOW_SEC, 1),
                "phase": phase_at(manifest, window_start + WINDOW_SEC / 2.0),
                "n_calls": stats["n_calls"],
                "lambda_base": round(stats["lambda_base"], 3),
                "lambda_retry": round(stats["lambda_retry"], 3),
                "rho": round(stats["rho"], 4),
                "s_useful": round(stats["s_useful"], 3),
                "concurrency": round(stats["concurrency"], 4),
                "p_success": "" if stats["p_success"] is None else round(stats["p_success"], 4),
                "fail_ratio": round(stats["fail_ratio"], 4),
                "overload_ratio": round(stats["overload_ratio"], 4),
                "mean_latency_ms": "" if stats["mean_latency_ms"] is None else round(stats["mean_latency_ms"], 2),
                "user_requests": n_users,
                "user_success_rate": user_success_rate,
                "cond1_rho_high": c1,
                "cond2_throughput_collapse": c2,
                "cond3_concurrency_stuck": c3,
                "cond4_positive_feedback": c4,
                "label": label,
            })
    return rows, baselines


def group_records_by_scenario(records):
    grouped = {}
    for record in records:
        scenario_id = record.get("scenario_id")
        if scenario_id not in grouped:
            grouped[scenario_id] = []
        grouped[scenario_id].append(record)
    return grouped


def select_manifests(group):
    manifests = []
    for manifest in read_scenarios():
        if manifest.get("interrupted"):
            continue
        if group == "all" or manifest.get("group") == group:
            manifests.append(manifest)
    return manifests


def label_all(manifests, th, call_by_scenario, user_by_scenario):
    all_rows = []
    all_baselines = []
    for manifest in manifests:
        scenario_id = manifest["scenario_id"]
        rows, baselines = label_scenario(manifest,
                                         within_run(call_by_scenario.get(scenario_id, []), manifest),
                                         within_run(user_by_scenario.get(scenario_id, []), manifest), th)
        all_rows.extend(rows)
        all_baselines.append(baselines)
    return all_rows, all_baselines


def global_baselines(all_baselines):
    # one healthy level per edge across all scenarios (median), saved for
    # the prediction model and the live controller
    result = {}
    for edge in EDGES:
        latency = []
        concurrency = []
        s_useful = []
        for baselines in all_baselines:
            if edge in baselines:
                latency.append(baselines[edge]["latency_ms"])
                concurrency.append(baselines[edge]["concurrency"])
                s_useful.append(baselines[edge]["s_useful"])
        if len(latency) == 0:
            continue
        result[edge_name(edge)] = {
            "latency_ms": median_of(latency),
            "concurrency": median_of(concurrency),
            "s_useful": median_of(s_useful),
        }
    return result


def output_path(group):
    if group == "dataset":
        return os.path.join(DATA_DIR, "labeled_windows.csv")
    return os.path.join(DATA_DIR, "labeled_windows_%s.csv" % group)


def write_rows(path, rows):
    with open(path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=FIELDNAMES)
        writer.writeheader()
        for row in rows:
            writer.writerow(row)


def summarize(rows, title):
    lines = []
    lines.append(title)
    lines.append("=" * len(title))

    scenarios = set()
    for row in rows:
        scenarios.add(row["scenario_id"])
    n_livelock = 0
    for row in rows:
        if row["label"] == "livelock":
            n_livelock = n_livelock + 1
    lines.append("scenarios: %d   windows (edge x 2s): %d   labelled livelock: %d"
                 % (len(scenarios), len(rows), n_livelock))
    lines.append("")

    lines.append("livelock windows by edge and phase (livelock / total)")
    phases = ["normal", "ramp", "fault", "recovery"]
    header = "  %-36s" % "edge"
    for phase in phases:
        header = header + " %13s" % phase
    lines.append(header)
    for edge in EDGES:
        line = "  %-36s" % edge_name(edge)
        for phase in phases:
            total = 0
            flagged = 0
            for row in rows:
                if (row["source_service"], row["dest_service"]) == edge and row["phase"] == phase:
                    total = total + 1
                    if row["label"] == "livelock":
                        flagged = flagged + 1
            line = line + " %13s" % ("%d / %d" % (flagged, total))
        lines.append(line)
    lines.append("")

    normal_total = 0
    normal_flagged = 0
    for row in rows:
        if row["phase"] == "normal":
            normal_total = normal_total + 1
            if row["label"] == "livelock":
                normal_flagged = normal_flagged + 1
    lines.append("false alarms during healthy traffic (normal phase): %d of %d windows"
                 % (normal_flagged, normal_total))

    # do the flagged windows really look like "busy but no progress"?
    # compare what USERS experienced in flagged vs unflagged windows,
    # only looking at fault/recovery time so the comparison is fair
    flagged_success = []
    other_success = []
    flagged_seen = set()
    other_seen = set()
    for row in rows:
        if row["phase"] not in ("ramp", "fault", "recovery"):
            continue
        if row["user_success_rate"] == "":
            continue
        key = (row["scenario_id"], row["window_index"])
        if row["label"] == "livelock":
            if key not in flagged_seen:
                flagged_seen.add(key)
                flagged_success.append(float(row["user_success_rate"]))
    for row in rows:
        if row["phase"] not in ("ramp", "fault", "recovery"):
            continue
        if row["user_success_rate"] == "":
            continue
        key = (row["scenario_id"], row["window_index"])
        if key not in flagged_seen and key not in other_seen:
            other_seen.add(key)
            other_success.append(float(row["user_success_rate"]))
    if len(flagged_success) > 0 and len(other_success) > 0:
        lines.append("users who arrived in a window flagged livelock on any edge got a success %.1f%% of the time"
                     % (100.0 * sum(flagged_success) / len(flagged_success)))
        lines.append("users who arrived in any other fault/recovery window got a success %.1f%% of the time"
                     % (100.0 * sum(other_success) / len(other_success)))
    return lines


def strategy_name(scenario_id):
    # exp_metastable_static_retry_r3 -> metastable / static_retry
    name = scenario_id
    if name.startswith("exp_"):
        name = name[4:]
    if "_r" in name:
        name = name[:name.rindex("_r")]
    return name


def per_strategy(rows):
    lines = []
    lines.append("")
    lines.append("livelock windows per experiment and retry strategy (all repetitions together)")
    counts = {}
    runs = {}
    flagged_runs = {}
    for row in rows:
        name = strategy_name(row["scenario_id"])
        if name not in counts:
            counts[name] = 0
            runs[name] = set()
            flagged_runs[name] = set()
        runs[name].add(row["scenario_id"])
        if row["label"] == "livelock":
            counts[name] = counts[name] + 1
            flagged_runs[name].add(row["scenario_id"])
    for name in sorted(counts):
        lines.append("  %-30s %4d windows, livelock formed in %d of %d runs"
                     % (name, counts[name], len(flagged_runs[name]), len(runs[name])))
    return lines


# ---------------- sensitivity analysis ----------------
#
# "How did you choose the thresholds?" -- here is what happens if each one
# is moved up or down. If the conclusions only held for one magic setting,
# this table would show it.

SWEEP = [
    ("RHO_CRIT", [0.5, 0.75, 1.0, 1.25, 1.5]),
    ("BETA", [0.3, 0.4, 0.5, 0.6, 0.7]),
    ("C_FACTOR", [1.5, 2.0, 3.0, 4.0]),
    ("TREND_TOL", [0.0, 0.1, 0.2, 0.3]),
    ("TAU_LOCK", [1, 2, 3, 4]),
]


def run_sensitivity(manifests, call_by_scenario, user_by_scenario):
    results = []
    for name, values in SWEEP:
        for value in values:
            th = dict(DEFAULT_THRESHOLDS)
            th[name] = value
            rows, baselines = label_all(manifests, th, call_by_scenario, user_by_scenario)
            counts = {"normal": [0, 0], "ramp": [0, 0], "fault": [0, 0], "recovery": [0, 0]}
            for row in rows:
                if row["phase"] in counts:
                    counts[row["phase"]][0] = counts[row["phase"]][0] + 1
                    if row["label"] == "livelock":
                        counts[row["phase"]][1] = counts[row["phase"]][1] + 1
            results.append({
                "parameter": name,
                "value": value,
                "is_default": value == DEFAULT_THRESHOLDS[name],
                "livelock_normal": counts["normal"][1],
                "livelock_ramp": counts["ramp"][1],
                "livelock_fault": counts["fault"][1],
                "livelock_recovery": counts["recovery"][1],
                "windows_normal": counts["normal"][0],
            })
    return results


def write_sensitivity(results):
    path = os.path.join(RESULTS_DIR, "sensitivity.csv")
    fieldnames = ["parameter", "value", "is_default", "livelock_normal", "livelock_ramp",
                  "livelock_fault", "livelock_recovery", "windows_normal"]
    with open(path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for result in results:
            writer.writerow(result)

    lines = []
    lines.append("Threshold sensitivity (one parameter moved at a time, others at default)")
    lines.append("=" * 72)
    lines.append("%-10s %7s   %14s %8s %8s %10s" % ("parameter", "value", "false alarms", "ramp", "fault", "recovery"))
    for result in results:
        mark = " <- default" if result["is_default"] else ""
        lines.append("%-10s %7s   %14s %8d %8d %10d%s" % (
            result["parameter"], result["value"],
            "%d / %d" % (result["livelock_normal"], result["windows_normal"]),
            result["livelock_ramp"], result["livelock_fault"], result["livelock_recovery"], mark))
    text = "\n".join(lines)
    with open(os.path.join(RESULTS_DIR, "sensitivity.txt"), "w") as f:
        f.write(text + "\n")
    return text


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--group", type=str, default="dataset",
                        help="which scenarios to label: dataset, experiment, manual, or all")
    parser.add_argument("--sensitivity", action="store_true",
                        help="also re-label with each threshold moved up and down")
    args = parser.parse_args()
    make_dirs()

    manifests = select_manifests(args.group)
    if len(manifests) == 0:
        print("no scenarios found in group '%s'." % args.group)
        print("run scenario_runner.py (or generate_dataset.py) first.")
        sys.exit(1)

    scenario_ids = []
    for manifest in manifests:
        scenario_ids.append(manifest["scenario_id"])

    call_records, skipped_calls = read_call_records(scenario_ids)
    user_records, skipped_users = read_user_records(scenario_ids)
    if skipped_calls + skipped_users > 0:
        print("warning: skipped %d damaged log line(s)" % (skipped_calls + skipped_users))
    print("loaded %d call records and %d user requests from %d scenario(s)"
          % (len(call_records), len(user_records), len(manifests)))

    call_by_scenario = group_records_by_scenario(call_records)
    user_by_scenario = group_records_by_scenario(user_records)

    rows, all_baselines = label_all(manifests, DEFAULT_THRESHOLDS, call_by_scenario, user_by_scenario)
    path = output_path(args.group)
    write_rows(path, rows)
    print("wrote %d labelled windows to %s" % (len(rows), path))

    if args.group == "dataset":
        baselines = global_baselines(all_baselines)
        with open(os.path.join(DATA_DIR, "baselines.json"), "w") as f:
            json.dump(baselines, f, indent=2)

    lines = summarize(rows, "Livelock detection -- group '%s'" % args.group)
    if args.group == "experiment":
        lines.extend(per_strategy(rows))
    text = "\n".join(lines)
    print("")
    print(text)
    with open(os.path.join(RESULTS_DIR, "labeling_%s.txt" % args.group), "w") as f:
        f.write(text + "\n")

    if args.sensitivity:
        print("")
        print("running the threshold sensitivity sweep...")
        results = run_sensitivity(manifests, call_by_scenario, user_by_scenario)
        print(write_sensitivity(results))


if __name__ == "__main__":
    main()
