import argparse
import csv
import json
import math
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from common.config import CODE_DIR, DATA_DIR, RESULTS_DIR, EDGES, edge_name, make_dirs
from common.telemetry import read_call_records, read_user_records, read_scenarios, within_run
from features import window_stats_for_edge, split_by_edge, WINDOW_SEC
from common.plots import (plt, save_figure, fault_span, BLUE, ORANGE, GREEN, RED, PURPLE, GREY,
                          LIVELOCK_SHADE)


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


PHASES = ["normal", "ramp", "fault", "recovery"]


def short_edge(edge):
    return edge_name(edge).replace("_service", "")


def count_by_edge_phase(rows):
    table = {}
    for edge in EDGES:
        table[edge] = {}
        for phase in PHASES:
            table[edge][phase] = [0, 0]
    for row in rows:
        edge = (row["source_service"], row["dest_service"])
        if edge in table and row["phase"] in table[edge]:
            cell = table[edge][row["phase"]]
            cell[0] = cell[0] + 1
            if row["label"] == "livelock":
                cell[1] = cell[1] + 1
    return table


def user_success_split(rows):
    flagged = {}
    for row in rows:
        if row["label"] == "livelock":
            flagged[(row["scenario_id"], row["window_index"])] = True
    flagged_rates = []
    other_rates = []
    seen = set()
    for row in rows:
        if row["phase"] not in ("ramp", "fault", "recovery") or row["user_success_rate"] == "":
            continue
        key = (row["scenario_id"], row["window_index"])
        if key in seen:
            continue
        seen.add(key)
        if key in flagged:
            flagged_rates.append(float(row["user_success_rate"]))
        else:
            other_rates.append(float(row["user_success_rate"]))
    flagged_mean = None
    other_mean = None
    if len(flagged_rates) > 0:
        flagged_mean = 100.0 * sum(flagged_rates) / len(flagged_rates)
    if len(other_rates) > 0:
        other_mean = 100.0 * sum(other_rates) / len(other_rates)
    return flagged_mean, other_mean


def summarize(rows, group):
    scenarios = set()
    n_livelock = 0
    for row in rows:
        scenarios.add(row["scenario_id"])
        if row["label"] == "livelock":
            n_livelock = n_livelock + 1

    lines = []
    lines.append("LIVELOCK DETECTION  |  group %s  |  %d runs  |  %d windows (edge x 2 s)  |  %d livelock"
                 % (group, len(scenarios), len(rows), n_livelock))
    lines.append("")
    lines.append("Table 1. Livelock windows / windows, by edge and phase")
    header = "%-20s" % "Edge"
    for phase in PHASES:
        header = header + " %10s" % phase.capitalize()
    lines.append(header)
    lines.append("-" * len(header))
    table = count_by_edge_phase(rows)
    for edge in EDGES:
        line = "%-20s" % short_edge(edge)
        for phase in PHASES:
            cell = table[edge][phase]
            line = line + " %10s" % ("%d/%d" % (cell[1], cell[0]))
        lines.append(line)
    lines.append("")

    normal_total = 0
    normal_flagged = 0
    for edge in EDGES:
        normal_total = normal_total + table[edge]["normal"][0]
        normal_flagged = normal_flagged + table[edge]["normal"][1]
    lines.append("False alarms (normal phase): %d/%d windows" % (normal_flagged, normal_total))
    flagged_mean, other_mean = user_success_split(rows)
    if flagged_mean is not None and other_mean is not None:
        lines.append("User success: livelock windows %.1f%%  |  other ramp/fault/recovery windows %.1f%%"
                     % (flagged_mean, other_mean))
    return lines


def strategy_name(scenario_id):
    name = scenario_id
    if name.startswith("exp_"):
        name = name[4:]
    if "_r" in name:
        name = name[:name.rindex("_r")]
    return name


def strategy_counts(rows):
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
    return counts, runs, flagged_runs


def strategy_label(name):
    for experiment in ["error_burst", "metastable"]:
        if name.startswith(experiment + "_"):
            return experiment.replace("_", " ") + "\n" + name[len(experiment) + 1:].replace("_", " ")
    return name


def per_strategy(rows):
    counts, runs, flagged_runs = strategy_counts(rows)
    lines = ["", "Table 2. Livelock by experiment and strategy"]
    header = "%-34s %10s %16s" % ("Experiment / strategy", "Windows", "Runs with LL")
    lines.append(header)
    lines.append("-" * len(header))
    for name in sorted(counts):
        lines.append("%-34s %10d %16s" % (name, counts[name], "%d/%d" % (len(flagged_runs[name]), len(runs[name]))))
    return lines


def window_of(row):
    return int(row["window_index"])


def example_edge(rows):
    counts = {}
    for row in rows:
        if row["label"] == "livelock":
            key = (row["scenario_id"], row["source_service"], row["dest_service"])
            counts[key] = counts.get(key, 0) + 1
    best = None
    for key in sorted(counts):
        if best is None or counts[key] > counts[best]:
            best = key
    return best


def draw(rows, group, show):
    table = count_by_edge_phase(rows)
    colours = [BLUE, ORANGE, GREEN]
    if group == "experiment":
        counts, runs, flagged_runs = strategy_counts(rows)
        names = sorted(counts)
        fig, ax = plt.subplots(figsize=(10, 4.2))
        xs = list(range(len(names)))
        ax.bar(xs, [counts[n] for n in names], color=[RED if counts[n] > 0 else GREY for n in names])
        ax.grid(False, axis="x")
        for x, n in zip(xs, names):
            ax.text(x, counts[n], "%d/%d runs" % (len(flagged_runs[n]), len(runs[n])), ha="center",
                    va="bottom", fontsize=8)
        ax.set_xticks(xs)
        ax.set_xticklabels([strategy_label(n) for n in names], fontsize=8)
        ax.set_ylabel("Livelock windows")
        ax.set_title("Livelock windows per experiment and strategy")
        fig.tight_layout()
        return save_figure(fig, "detection_%s.png" % group, show)

    fig = plt.figure(figsize=(11, 9))
    grid = fig.add_gridspec(3, 2, height_ratios=[1.1, 1, 1])
    ax_a = fig.add_subplot(grid[0, 0])
    ax_b = fig.add_subplot(grid[0, 1])
    ax_c = fig.add_subplot(grid[1, :])
    ax_d = fig.add_subplot(grid[2, 0])
    ax_e = fig.add_subplot(grid[2, 1])

    width = 0.26
    for k in range(len(EDGES)):
        edge = EDGES[k]
        values = [table[edge][phase][1] for phase in PHASES]
        xs = [i + (k - 1) * width for i in range(len(PHASES))]
        ax_a.bar(xs, values, width=width, color=colours[k], label=short_edge(edge))
        ax_a.grid(False, axis="x")
    ax_a.set_xticks(range(len(PHASES)))
    ax_a.set_xticklabels([p.capitalize() for p in PHASES])
    ax_a.set_ylabel("Livelock windows")
    ax_a.set_title("(a) Livelock windows by phase")
    ax_a.grid(axis="x", visible=False)
    ax_a.legend()

    flagged_mean, other_mean = user_success_split(rows)
    if flagged_mean is not None and other_mean is not None:
        bars = ax_b.bar([0, 1], [flagged_mean, other_mean], color=[RED, BLUE], width=0.55)
        ax_b.grid(False, axis="x")
        for bar, value in zip(bars, [flagged_mean, other_mean]):
            ax_b.text(bar.get_x() + bar.get_width() / 2.0, value + 1.5, "%.1f%%" % value, ha="center", fontsize=10)
    ax_b.set_xticks([0, 1])
    ax_b.set_xticklabels(["Livelock windows", "Other fault windows"])
    ax_b.set_ylim(0, 100)
    ax_b.set_ylabel("User success (%)")
    ax_b.set_title("(b) User success")
    ax_b.grid(axis="x", visible=False)

    best = example_edge(rows)
    if best is not None:
        scenario_id, source, dest = best
        manifest = None
        for m in read_scenarios():
            if m["scenario_id"] == scenario_id and not m.get("interrupted"):
                manifest = m
        edge_rows = [r for r in rows if r["scenario_id"] == scenario_id and r["source_service"] == source
                     and r["dest_service"] == dest]
        edge_rows.sort(key=window_of)
        warm = [r for r in edge_rows if float(r["t_rel"]) < WARMUP_SEC and int(r["n_calls"]) >= MIN_CALLS]
        base_s = median_of([float(r["s_useful"]) for r in warm]) or 1.0
        base_c = median_of([float(r["concurrency"]) for r in warm]) or 1.0
        xs = [float(r["t_rel"]) + WINDOW_SEC / 2.0 for r in edge_rows]
        for ax in (ax_c, ax_d, ax_e):
            for r in edge_rows:
                if r["label"] == "livelock":
                    ax.axvspan(float(r["t_rel"]), float(r["t_rel"]) + WINDOW_SEC, color=LIVELOCK_SHADE,
                               linewidth=0, zorder=0)
            if manifest is not None:
                start, end = fault_span(manifest)
                ax.axvline(start - manifest["start"], color=GREY, linestyle=":", linewidth=1.2)
                ax.axvline(end - manifest["start"], color=GREY, linestyle=":", linewidth=1.2)
        if manifest is not None:
            start, end = fault_span(manifest)
            ax_c.text(start - manifest["start"], 1.01, "fault on", transform=ax_c.get_xaxis_transform(),
                      ha="center", va="bottom", fontsize=8, color=GREY)
            ax_c.text(end - manifest["start"], 1.01, "fault off", transform=ax_c.get_xaxis_transform(),
                      ha="center", va="bottom", fontsize=8, color=GREY)
        ax_c.plot(xs, [float(r["rho"]) for r in edge_rows], color=ORANGE, marker="o", markersize=3)
        ax_c.axhline(RHO_CRIT, color=ORANGE, linestyle="--", linewidth=1)
        ax_c.set_ylabel("rho")
        ax_c.set_title("(c) %s, %s -> %s: retry amplification rho (C1), shaded = livelock"
                       % (scenario_id, source, dest.replace("_service", "")), pad=14)
        ax_d.plot(xs, [float(r["s_useful"]) / base_s for r in edge_rows], color=GREEN, marker="o", markersize=3)
        ax_d.axhline(BETA, color=GREEN, linestyle="--", linewidth=1)
        ax_d.set_ylabel("S / S*")
        ax_d.set_xlabel("Time (s)")
        ax_d.set_title("(d) Useful throughput (C2)")
        ax_e.plot(xs, [float(r["concurrency"]) / base_c for r in edge_rows], color=PURPLE, marker="o",
                  markersize=3)
        ax_e.axhline(C_FACTOR, color=PURPLE, linestyle="--", linewidth=1)
        ax_e.set_ylabel("N / N*")
        ax_e.set_xlabel("Time (s)")
        ax_e.set_title("(e) Calls in flight (C3)")
    fig.tight_layout()
    return save_figure(fig, "detection_%s.png" % group, show)


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
    lines.append("Table 3. Threshold sensitivity (livelock windows; one parameter changed at a time)")
    header = "%-10s %7s %14s %8s %8s %10s" % ("Parameter", "Value", "Normal (FA)", "Ramp", "Fault", "Recovery")
    lines.append(header)
    lines.append("-" * len(header))
    for result in results:
        mark = "  *" if result["is_default"] else ""
        lines.append("%-10s %7s %14s %8d %8d %10d%s" % (
            result["parameter"], result["value"],
            "%d/%d" % (result["livelock_normal"], result["windows_normal"]),
            result["livelock_ramp"], result["livelock_fault"], result["livelock_recovery"], mark))
    text = "\n".join(lines)
    with open(os.path.join(RESULTS_DIR, "sensitivity.txt"), "w") as f:
        f.write(text + "\n")
    return text


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--group", type=str, default="dataset")
    parser.add_argument("--sensitivity", action="store_true")
    parser.add_argument("--no_show", action="store_true")
    args = parser.parse_args()
    make_dirs()

    manifests = select_manifests(args.group)
    if len(manifests) == 0:
        print("error: no runs in group '%s'" % args.group)
        sys.exit(1)

    scenario_ids = []
    for manifest in manifests:
        scenario_ids.append(manifest["scenario_id"])

    call_records, skipped_calls = read_call_records(scenario_ids)
    user_records, skipped_users = read_user_records(scenario_ids)
    call_by_scenario = group_records_by_scenario(call_records)
    user_by_scenario = group_records_by_scenario(user_records)

    rows, all_baselines = label_all(manifests, DEFAULT_THRESHOLDS, call_by_scenario, user_by_scenario)
    path = output_path(args.group)
    write_rows(path, rows)

    if args.group == "dataset":
        baselines = global_baselines(all_baselines)
        with open(os.path.join(DATA_DIR, "baselines.json"), "w") as f:
            json.dump(baselines, f, indent=2)

    lines = summarize(rows, args.group)
    if args.group == "experiment":
        lines.extend(per_strategy(rows))
    text = "\n".join(lines)
    print(text)
    with open(os.path.join(RESULTS_DIR, "labeling_%s.txt" % args.group), "w") as f:
        f.write(text + "\n")

    if args.sensitivity:
        results = run_sensitivity(manifests, call_by_scenario, user_by_scenario)
        print("")
        print(write_sensitivity(results))

    print("")
    print("Labels: %s" % os.path.relpath(path, CODE_DIR))
    draw(rows, args.group, not args.no_show)


if __name__ == "__main__":
    main()
