import csv
import json
import math
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.ticker import MaxNLocator

from common.config import DATA_DIR, RESULTS_DIR, FIGURES_DIR, DEFAULT_SLOTS, make_dirs
from common.telemetry import (read_call_records, read_user_records, read_server_records,
                              read_scenarios, read_jsonl, data_path, CONTROLLER_LOG_NAME, within_run)

# Draws every figure used in the report / slides into results/figures/.
# Each figure is skipped (with a message) if the data it needs is missing.

# colour-blind-safe categorical colours (validated), used in a fixed order
BLUE = "#2a78d6"
ORANGE = "#eb6834"
AQUA = "#1baf7a"
GREY = "#a8a7a2"
INK = "#2b2b2b"
MUTED = "#6b6a66"
GRID = "#e6e5e1"
FAULT_SHADE = "#f3e3dc"
LIVELOCK_SHADE = "#f6c9c4"

TIMELINE_ARMS = [("static_retry", "static retry (3 attempts)", ORANGE),
                 ("retry_budget", "retry budget (10%)", AQUA),
                 ("predictive", "our predictive controller", BLUE)]

ARM_ORDER = ["no_retry", "static_retry", "retry_budget", "reactive", "predictive"]
ARM_LABELS = {"no_retry": "no retry", "static_retry": "static\nretry", "retry_budget": "retry\nbudget",
              "reactive": "reactive\n(ours)", "predictive": "predictive\n(ours)"}


def style(ax):
    ax.spines["top"].set_visible(False)
    ax.spines["right"].set_visible(False)
    ax.spines["left"].set_color(GRID)
    ax.spines["bottom"].set_color(GRID)
    ax.tick_params(colors=MUTED, labelsize=8)
    ax.yaxis.grid(True, color=GRID, linewidth=0.8)
    ax.set_axisbelow(True)


def save(fig, name):
    path = os.path.join(FIGURES_DIR, name)
    fig.savefig(path, dpi=160, bbox_inches="tight", facecolor="white")
    plt.close(fig)
    print("  wrote results/figures/%s" % name)


def find_manifest(scenario_id):
    found = None
    for manifest in read_scenarios():
        if manifest["scenario_id"] == scenario_id and not manifest.get("interrupted"):
            found = manifest
    return found


def fault_span(manifest):
    start = None
    end = None
    for phase in manifest["phases"]:
        if phase["name"] in ("ramp", "fault"):
            if start is None:
                start = phase["start"]
            end = phase["end"]
    return start, end


def shade_fault(ax, manifest, t0):
    start, end = fault_span(manifest)
    if start is None:
        return
    ax.axvspan(start - t0, end - t0, color=FAULT_SHADE, linewidth=0)


def bucket_users(users, t0, seconds, width):
    n = int(math.ceil(seconds / width))
    totals = [0] * n
    oks = [0] * n
    for record in users:
        k = int(math.floor((record["start_time"] - t0) / width))
        if 0 <= k < n:
            totals[k] = totals[k] + 1
            if record["succeeded"]:
                oks[k] = oks[k] + 1
    xs = []
    ys = []
    for k in range(n):
        if totals[k] > 0:
            xs.append(k * width + width / 2.0)
            ys.append(100.0 * oks[k] / totals[k])
    return xs, ys


def bucket_calls(calls, t0, seconds, width):
    n = int(math.ceil(seconds / width))
    counts = [0] * n
    for record in calls:
        k = int(math.floor((record.get("start_time", record["timestamp"]) - t0) / width))
        if 0 <= k < n:
            counts[k] = counts[k] + 1
    xs = []
    ys = []
    for k in range(n):
        xs.append(k * width + width / 2.0)
        ys.append(counts[k] / width)
    return xs, ys


def timeline_figure(experiment, rep, name, title):
    manifests = {}
    for arm, label, colour in TIMELINE_ARMS:
        manifest = find_manifest("exp_%s_%s_r%d" % (experiment, arm, rep))
        if manifest is None:
            print("  skipped %s: run exp_%s_%s_r%d not found" % (name, experiment, arm, rep))
            return
        manifests[arm] = manifest

    fig, axes = plt.subplots(2, 1, figsize=(10, 6.2), sharex=True)
    first = manifests[TIMELINE_ARMS[0][0]]
    seconds = first["end"] - first["start"]
    for ax in axes:
        shade_fault(ax, first, first["start"])
        style(ax)

    for arm, label, colour in TIMELINE_ARMS:
        manifest = manifests[arm]
        users, s = read_user_records([manifest["scenario_id"]])
        calls, s = read_call_records([manifest["scenario_id"]])
        users = within_run(users, manifest)
        calls = within_run(calls, manifest)
        xs, ys = bucket_users(users, manifest["start"], seconds, 2.0)
        axes[0].plot(xs, ys, color=colour, linewidth=2, label=label, solid_capstyle="round")
        xs, ys = bucket_calls(calls, manifest["start"], seconds, 2.0)
        axes[1].plot(xs, ys, color=colour, linewidth=2, label=label, solid_capstyle="round")

    start, end = fault_span(first)
    axes[0].text((start - first["start"] + end - first["start"]) / 2.0, 104, "fault active",
                 ha="center", fontsize=8, color=MUTED)
    axes[0].text(end - first["start"] + 1, 104, "fault removed", ha="left", fontsize=8, color=MUTED)
    axes[0].set_ylim(-3, 110)
    axes[0].set_ylabel("users who got a success (%)", fontsize=9, color=INK)
    axes[1].set_ylabel("internal calls per second", fontsize=9, color=INK)
    axes[1].set_xlabel("seconds since the run started", fontsize=9, color=INK)
    axes[0].legend(loc="lower left", fontsize=8, frameon=False)
    fig.suptitle(title, fontsize=11, color=INK)
    fig.tight_layout()
    save(fig, name)


def load_runs():
    path = os.path.join(RESULTS_DIR, "experiment_runs.csv")
    if not os.path.exists(path):
        return []
    with open(path, "r") as f:
        rows = list(csv.DictReader(f))
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


def summary_figure(rows):
    if len(rows) == 0:
        print("  skipped experiment summary: results/experiment_runs.csv not found")
        return
    experiments = []
    for row in rows:
        if row["experiment"] not in experiments:
            experiments.append(row["experiment"])
    metrics = [("normal_success_pct", "users served, healthy period (%)", "%.0f"),
               ("fault_success_pct", "users served during the fault (%)", "%.0f"),
               ("recovery_success_pct", "users served after fault removed (%)", "%.0f"),
               ("fault_calls", "internal calls during the fault", "%.0f"),
               ("recovery_sec", "seconds to recover", "%.0f")]

    fig, axes = plt.subplots(len(experiments), len(metrics), figsize=(18.5, 3.7 * len(experiments)))
    if len(experiments) == 1:
        axes = [axes]
    for i in range(len(experiments)):
        experiment = experiments[i]
        for j in range(len(metrics)):
            key, title, form = metrics[j]
            ax = axes[i][j]
            style(ax)
            xs = []
            heights = []
            errors = []
            colours = []
            never = []
            for k in range(len(ARM_ORDER)):
                arm = ARM_ORDER[k]
                arm_rows = [r for r in rows if r["experiment"] == experiment and r["arm"] == arm]
                values = [float(r[key]) for r in arm_rows]
                if len(values) == 0:
                    continue
                mean, std = mean_std(values)
                xs.append(k)
                heights.append(mean)
                errors.append(std)
                colours.append(BLUE if arm in ("reactive", "predictive") else GREY)
                recovered = [r for r in arm_rows if r["recovered"] == "True"]
                if key != "recovery_sec":
                    never.append(None)
                else:
                    never.append((len(recovered), len(arm_rows)))
            ax.bar(xs, heights, width=0.55, color=colours, yerr=errors, capsize=3,
                   error_kw={"ecolor": MUTED, "elinewidth": 1})
            top = max([heights[n] + errors[n] for n in range(len(heights))] + [1.0])
            for n in range(len(xs)):
                text = form % heights[n]
                if never[n] is not None and max(heights) > 0:
                    n_ok, n_all = never[n]
                    if n_ok == 0:
                        text = "never\n(>%s)" % text
                    elif n_ok < n_all:
                        # runs that never recovered are counted at the full window
                        text = "%s\n%d/%d recovered" % (text, n_ok, n_all)
                ax.text(xs[n], heights[n] + errors[n] + top * 0.03, text,
                        ha="center", fontsize=8, color=INK)
            ax.set_ylim(0, top * 1.25)
            if key == "recovery_sec" and max(heights) == 0:
                ax.text(2, 0.5, "every strategy recovered\nas soon as the fault ended", ha="center",
                        va="center", fontsize=9, color=MUTED)
                ax.set_yticks([])
            ax.set_xticks(range(len(ARM_ORDER)))
            ax.set_xticklabels([ARM_LABELS[a] for a in ARM_ORDER], fontsize=7.5)
            ax.set_title(title, fontsize=9, color=INK)
            if j == 0:
                ax.set_ylabel(experiment, fontsize=10, color=INK)
    fig.suptitle("Five retry strategies, same fault, 5 runs each (bars = mean, whiskers = std; "
                 "a run that never recovered counts as the full 25 s window)",
                 fontsize=11, color=INK)
    fig.tight_layout()
    save(fig, "fig3_experiment_summary.png")


def detection_figure():
    path = os.path.join(DATA_DIR, "labeled_windows.csv")
    if not os.path.exists(path):
        print("  skipped detection figure: data/labeled_windows.csv not found")
        return
    with open(path, "r") as f:
        rows = list(csv.DictReader(f))

    # pick the scenario + edge with the most livelock windows
    counts = {}
    for row in rows:
        if row["label"] == "livelock":
            key = (row["scenario_id"], row["source_service"], row["dest_service"])
            counts[key] = counts.get(key, 0) + 1
    if len(counts) == 0:
        print("  skipped detection figure: no livelock windows in the dataset")
        return
    best = None
    for key in counts:
        if best is None or counts[key] > counts[best]:
            best = key
    scenario_id, source, dest = best
    manifest = find_manifest(scenario_id)

    edge_rows = [r for r in rows if r["scenario_id"] == scenario_id
                 and r["source_service"] == source and r["dest_service"] == dest]
    edge_rows.sort(key=row_index)

    risk = {}
    oof_path = os.path.join(RESULTS_DIR, "prediction_oof.csv")
    if os.path.exists(oof_path):
        with open(oof_path, "r") as f:
            for r in csv.DictReader(f):
                if r["scenario_id"] == scenario_id and r["edge"] == source + "->" + dest:
                    risk[int(r["window_index"])] = float(r["risk"])

    xs = [float(r["t_rel"]) + 1.0 for r in edge_rows]
    warm = [r for r in edge_rows if float(r["t_rel"]) < 8.0 and int(r["n_calls"]) >= 4]
    base_s = median([float(r["s_useful"]) for r in warm])
    base_c = median([float(r["concurrency"]) for r in warm])

    panels = [("rho", "C1: retry amplification rho", 1.0, "rho = 1"),
              ("s_useful", "C2: useful throughput (calls/s)", 0.5 * base_s, "half of healthy"),
              ("concurrency", "C3: calls in flight", max(2.0 * base_c, 1.0), "2x healthy")]
    n_panels = len(panels) + (1 if len(risk) > 0 else 0)
    fig, axes = plt.subplots(n_panels, 1, figsize=(10, 2.0 * n_panels + 0.8), sharex=True)
    for ax in axes:
        style(ax)
        for r in edge_rows:
            if r["label"] == "livelock":
                ax.axvspan(float(r["t_rel"]), float(r["t_rel"]) + 2.0, color=LIVELOCK_SHADE, linewidth=0)
        start, end = fault_span(manifest)
        ax.axvline(start - manifest["start"], color=MUTED, linewidth=1)
        ax.axvline(end - manifest["start"], color=MUTED, linewidth=1)

    for k in range(len(panels)):
        key, title, line, line_label = panels[k]
        ys = [float(r[key]) for r in edge_rows]
        axes[k].plot(xs, ys, color=BLUE, linewidth=2)
        axes[k].axhline(line, color=ORANGE, linewidth=1.2)
        axes[k].text(xs[-1], line, " " + line_label, fontsize=7.5, color=MUTED, va="bottom", ha="right")
        axes[k].set_ylabel(title, fontsize=8, color=INK)
    if len(risk) > 0:
        rx = []
        ry = []
        for r in edge_rows:
            index = int(r["window_index"])
            if index in risk:
                rx.append(float(r["t_rel"]) + 1.0)
                ry.append(risk[index])
        axes[-1].plot(rx, ry, color=BLUE, linewidth=2)
        axes[-1].axhline(0.5, color=ORANGE, linewidth=1.2)
        axes[-1].set_ylim(-0.05, 1.05)
        axes[-1].set_ylabel("predicted risk\n(next 2-12 s)", fontsize=8, color=INK)
    axes[-1].set_xlabel("seconds since the run started   (vertical lines: fault starts / is removed;"
                        " red bands: windows labelled livelock)", fontsize=8, color=INK)
    fig.suptitle("Detection on %s, edge %s -> %s (%s %s fault, order queue %.0fs)"
                 % (scenario_id, source, dest, manifest["mode"], manifest["fault"], manifest["queue_wait"]),
                 fontsize=10, color=INK)
    fig.tight_layout()
    save(fig, "fig4_detection_example.png")


def row_index(row):
    return int(row["window_index"])


def median(values):
    if len(values) == 0:
        return 0.0
    ordered = sorted(values)
    middle = len(ordered) // 2
    if len(ordered) % 2 == 1:
        return ordered[middle]
    return (ordered[middle - 1] + ordered[middle]) / 2.0


def prediction_figure():
    path = os.path.join(RESULTS_DIR, "prediction_metrics.json")
    if not os.path.exists(path):
        print("  skipped prediction figure: results/prediction_metrics.json not found")
        return
    with open(path, "r") as f:
        metrics = json.load(f)

    names = [("logistic regression (ours)", "our model"),
             ("without graph features", "ours, no graph features"),
             ("without temporal features", "ours, no temporal features"),
             ("random forest (comparison)", "random forest"),
             ("rule: storm right now (reactive)", "reactive rule (no learning)"),
             ("rule: livelock right now (persistence)", "rule: livelock now"),
             ("shuffled-label control", "shuffled-label control")]

    fig, axes = plt.subplots(1, 2, figsize=(12, 4.2))
    for p in range(2):
        subset_key = "all" if p == 0 else "onset"
        ax = axes[p]
        style(ax)
        ax.xaxis.grid(True, color=GRID, linewidth=0.8)
        ax.yaxis.grid(False)
        labels = []
        values = []
        colours = []
        for key, label in names:
            if key in metrics[subset_key]:
                labels.append(label)
                values.append(metrics[subset_key][key]["f1"])
                colours.append(BLUE if key.startswith("logistic") else GREY)
        ys = list(range(len(labels)))
        ys.reverse()
        ax.barh(ys, values, height=0.55, color=colours)
        for n in range(len(values)):
            ax.text(values[n] + 0.01, ys[n], "%.2f" % values[n], va="center", fontsize=8, color=INK)
        ax.set_yticks(ys)
        ax.set_yticklabels(labels, fontsize=8)
        ax.set_xlim(0, 1.08)
        if p == 0:
            ax.set_title("F1, all windows (unseen scenarios)", fontsize=9, color=INK)
        else:
            ax.set_title("F1, before a livelock has started (onset only)", fontsize=9, color=INK)
    fig.suptitle("Predicting livelock 2-12 s ahead: our model against baselines and ablations",
                 fontsize=11, color=INK)
    fig.tight_layout()
    save(fig, "fig5_prediction.png")

    ours = metrics["early_warning"].get("our model")
    rule = metrics["early_warning"].get("reactive rule")
    if ours is None or rule is None:
        return
    fig, ax = plt.subplots(figsize=(7, 3.6))
    style(ax)
    bins = [0, 2, 4, 6, 8, 10]
    width = 0.8
    for offset, data, colour, label in [(-0.45, ours, BLUE, "our model"), (0.45, rule, ORANGE, "reactive rule")]:
        counts = []
        for b in bins[1:]:
            counts.append(len([x for x in data["lead_times"] if abs(x - b) < 0.01]))
        xs = [b + offset for b in bins[1:]]
        ax.bar(xs, counts, width=width, color=colour,
               label="%s: warned in %d/%d episodes" % (label, len(data["lead_times"]), data["episodes"]))
    ax.set_xticks(bins[1:])
    ax.yaxis.set_major_locator(MaxNLocator(integer=True))
    ax.set_xlabel("seconds of warning before the livelock started", fontsize=9, color=INK)
    ax.set_ylabel("livelock episodes", fontsize=9, color=INK)
    ax.legend(fontsize=8, frameon=False)
    ax.set_title("How early the alarm went up", fontsize=10, color=INK)
    fig.tight_layout()
    save(fig, "fig5b_lead_time.png")


def controller_figure(experiment, rep):
    scenario_id = "exp_%s_predictive_r%d" % (experiment, rep)
    manifest = find_manifest(scenario_id)
    records, skipped = read_jsonl(data_path(CONTROLLER_LOG_NAME))
    records = [r for r in records if r.get("tag") == scenario_id]
    if manifest is None or len(records) == 0:
        print("  skipped controller figure: no controller log for %s" % scenario_id)
        return
    t0 = manifest["start"]
    edges = [("gateway->order_service", "gateway -> order", BLUE),
             ("order_service->payment_service", "order -> payment", ORANGE),
             ("order_service->inventory_service", "order -> inventory", AQUA)]

    fig, axes = plt.subplots(2, 1, figsize=(10, 5.2), sharex=True)
    for ax in axes:
        style(ax)
        shade_fault(ax, manifest, t0)
    for name, label, colour in edges:
        rows = [r for r in records if r["edge"] == name]
        xs = [r["timestamp"] - t0 for r in rows]
        axes[0].step(xs, [r["max_attempts"] for r in rows], where="post", color=colour, linewidth=2, label=label)
        risk_x = [r["timestamp"] - t0 for r in rows if r["risk"] is not None]
        risk_y = [r["risk"] for r in rows if r["risk"] is not None]
        axes[1].plot(risk_x, risk_y, color=colour, linewidth=2, label=label)
    axes[0].set_ylabel("max attempts allowed", fontsize=9, color=INK)
    axes[0].set_yticks([1, 2, 3])
    axes[0].set_ylim(0.7, 3.4)
    axes[0].legend(fontsize=8, frameon=False, loc="lower left")
    axes[1].axhline(0.5, color=MUTED, linewidth=1)
    axes[1].set_ylim(-0.05, 1.05)
    axes[1].set_ylabel("predicted livelock risk", fontsize=9, color=INK)
    axes[1].set_xlabel("seconds since the run started (shaded: fault active)", fontsize=9, color=INK)
    fig.suptitle("What the predictive controller did (%s, run %d)" % (experiment, rep), fontsize=11, color=INK)
    fig.tight_layout()
    save(fig, "fig6_controller_trace.png")


def busy_no_progress_figure(experiment, rep):
    scenario_id = "exp_%s_static_retry_r%d" % (experiment, rep)
    manifest = find_manifest(scenario_id)
    if manifest is None:
        print("  skipped busy-but-no-progress figure: %s not found" % scenario_id)
        return
    t0 = manifest["start"]
    seconds = manifest["end"] - t0
    servers, s = read_server_records([scenario_id])
    users, s = read_user_records([scenario_id])
    servers = within_run(servers, manifest)
    users = within_run(users, manifest)

    width = 2.0
    n = int(math.ceil(seconds / width))
    busy = [0.0] * n
    done = [0] * n
    orphaned = [0] * n
    slots = DEFAULT_SLOTS["order_service"]
    for record in servers:
        if record["service"] != "order_service" or record["status"] == 503:
            continue
        start = record["slot_time"]
        end = record["timestamp"]
        first = int(math.floor((start - t0) / width))
        last = int(math.floor((end - t0) / width))
        for k in range(max(first, 0), min(last, n - 1) + 1):
            w = t0 + k * width
            overlap = min(end, w + width) - max(start, w)
            if overlap > 0:
                busy[k] = busy[k] + overlap
        k = int(math.floor((start - t0) / width))
        if 0 <= k < n:
            done[k] = done[k] + 1
            if record["orphaned"]:
                orphaned[k] = orphaned[k] + 1

    xs = [k * width + width / 2.0 for k in range(n)]
    util = [100.0 * busy[k] / (slots * width) for k in range(n)]
    orphan_x = [xs[k] for k in range(n) if done[k] > 0]
    orphan_y = [100.0 * orphaned[k] / done[k] for k in range(n) if done[k] > 0]
    ux, uy = bucket_users(users, t0, seconds, width)

    fig, axes = plt.subplots(3, 1, figsize=(10, 6.6), sharex=True)
    for ax in axes:
        style(ax)
        shade_fault(ax, manifest, t0)
        ax.set_ylim(-3, 108)
    axes[0].plot(xs, util, color=BLUE, linewidth=2)
    axes[0].set_ylabel("order_service\nslots busy (%)", fontsize=9, color=INK)
    axes[1].plot(orphan_x, orphan_y, color=ORANGE, linewidth=2)
    axes[1].set_ylabel("order_service work\nfor nobody (%)", fontsize=9, color=INK)
    axes[2].plot(ux, uy, color=AQUA, linewidth=2)
    axes[2].set_ylabel("users who got\na success (%)", fontsize=9, color=INK)
    axes[2].set_xlabel("seconds since the run started (shaded: fault active, then removed)",
                       fontsize=9, color=INK)
    fig.suptitle("Busy but no progress: static retries, %s experiment, run %d" % (experiment, rep),
                 fontsize=11, color=INK)
    fig.tight_layout()
    save(fig, "fig2_busy_no_progress.png")


def main():
    make_dirs()
    print("drawing figures...")
    timeline_figure("metastable", 1, "fig1_metastable_timeline.png",
                    "Self-sustaining retry storm: same fault, three retry strategies (run 1)")
    busy_no_progress_figure("metastable", 1)
    timeline_figure("error_burst", 1, "fig1b_error_burst_timeline.png",
                    "Error burst: same fault, three retry strategies (run 1)")
    summary_figure(load_runs())
    detection_figure()
    prediction_figure()
    controller_figure("metastable", 1)


if __name__ == "__main__":
    main()
