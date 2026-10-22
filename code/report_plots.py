import os
import sys
import math

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.ticker import MaxNLocator
from sklearn.metrics import roc_curve, roc_auc_score, precision_recall_curve, average_precision_score

from common.config import DATA_DIR, RESULTS_DIR, DEFAULT_SLOTS
from common.telemetry import (read_call_records, read_user_records, read_server_records,
                              read_jsonl, data_path, CONTROLLER_LOG_NAME, within_run)
import make_figures as mf
import evaluate_model as ev
import prediction_model


OUT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "report", "figs")

plt.rcParams.update({
    "font.family": "serif",
    "font.serif": ["Times New Roman", "Liberation Serif", "TeX Gyre Termes", "DejaVu Serif"],
    "mathtext.fontset": "stix",
    "font.size": 8,
    "axes.labelsize": 8,
    "legend.fontsize": 7,
    "xtick.labelsize": 7,
    "ytick.labelsize": 7,
    "axes.linewidth": 0.6,
    "lines.linewidth": 1.0,
    "lines.markersize": 3,
    "xtick.direction": "in",
    "ytick.direction": "in",
    "xtick.top": True,
    "ytick.right": True,
    "xtick.major.width": 0.6,
    "ytick.major.width": 0.6,
    "axes.grid": True,
    "grid.linestyle": ":",
    "grid.linewidth": 0.5,
    "grid.color": "0.65",
    "legend.frameon": True,
    "legend.fancybox": False,
    "legend.edgecolor": "black",
    "legend.framealpha": 1.0,
    "patch.linewidth": 0.5,
    "pdf.fonttype": 42,
})

ONE_COLUMN = 3.45
TWO_COLUMNS = 7.1
FAULT_GREY = "0.88"

STRATEGY_LINES = [("static_retry", "static retry", "C3", "--", "s"),
                  ("retry_budget", "retry budget", "C2", "-.", "^"),
                  ("predictive", "predictive (ours)", "C0", "-", "o")]

EDGE_LINES = [("gateway->order_service", "e1 gateway-order", "C0", "-", "o"),
              ("order_service->payment_service", "e2 order-payment", "C3", "--", "s"),
              ("order_service->inventory_service", "e3 order-inventory", "C2", ":", "^")]


def save(fig, name):
    if not os.path.exists(OUT_DIR):
        os.makedirs(OUT_DIR)
    path = os.path.join(OUT_DIR, name)
    fig.savefig(path, bbox_inches="tight", pad_inches=0.02)
    plt.close(fig)
    print("  wrote report/figs/%s" % name)


def shade_fault(ax, manifest):
    start, end = mf.fault_span(manifest)
    if start is not None:
        ax.axvspan(start - manifest["start"], end - manifest["start"], color=FAULT_GREY, linewidth=0, zorder=0)


def timeline_plot(experiment, rep, name):
    manifests = {}
    for arm, label, colour, dash, marker in STRATEGY_LINES:
        m = mf.find_manifest("exp_%s_%s_r%d" % (experiment, arm, rep))
        if m is None:
            print("  skipped %s (run not found)" % name)
            return
        manifests[arm] = m
    first = manifests["static_retry"]
    seconds = first["end"] - first["start"]

    fig, axes = plt.subplots(2, 1, figsize=(ONE_COLUMN, 2.9), sharex=True)
    for ax in axes:
        shade_fault(ax, first)
    for arm, label, colour, dash, marker in STRATEGY_LINES:
        m = manifests[arm]
        users, s = read_user_records([m["scenario_id"]])
        calls, s = read_call_records([m["scenario_id"]])
        users = within_run(users, m)
        calls = within_run(calls, m)
        xs, ys = mf.bucket_users(users, m["start"], seconds, 2.0)
        axes[0].plot(xs, ys, color=colour, linestyle=dash, marker=marker, markevery=2, label=label)
        xs, ys = mf.bucket_calls(calls, m["start"], seconds, 2.0)
        axes[1].plot(xs, ys, color=colour, linestyle=dash, marker=marker, markevery=2, label=label)

    start, end = mf.fault_span(first)
    axes[0].text((start + end) / 2.0 - first["start"], 50, "fault", ha="center", va="center", fontsize=7)
    axes[0].set_ylim(-5, 108)
    axes[0].set_ylabel("Users served (%)")
    axes[1].set_ylabel("Internal calls/s")
    axes[1].set_xlabel("Time (s)")
    axes[1].set_xlim(0, seconds)
    handles, labels = axes[0].get_legend_handles_labels()
    fig.legend(handles, labels, loc="lower center", ncol=3, bbox_to_anchor=(0.55, 0.98),
               handlelength=2.2, columnspacing=1.0)
    fig.tight_layout(h_pad=0.4)
    save(fig, name)


def busy_plot(experiment, rep):
    scenario_id = "exp_%s_static_retry_r%d" % (experiment, rep)
    m = mf.find_manifest(scenario_id)
    if m is None:
        print("  skipped busy plot")
        return
    t0 = m["start"]
    seconds = m["end"] - t0
    servers, s = read_server_records([scenario_id])
    users, s = read_user_records([scenario_id])
    servers = within_run(servers, m)
    users = within_run(users, m)

    width = 2.0
    n = int(math.ceil(seconds / width))
    busy = [0.0] * n
    done = [0] * n
    orphaned = [0] * n
    slots = DEFAULT_SLOTS["order_service"]
    for r in servers:
        if r["service"] != "order_service" or r["status"] == 503:
            continue
        a = r["slot_time"]
        b = r["timestamp"]
        for k in range(max(int(math.floor((a - t0) / width)), 0), min(int(math.floor((b - t0) / width)), n - 1) + 1):
            w = t0 + k * width
            overlap = min(b, w + width) - max(a, w)
            if overlap > 0:
                busy[k] = busy[k] + overlap
        k = int(math.floor((a - t0) / width))
        if 0 <= k < n:
            done[k] = done[k] + 1
            if r["orphaned"]:
                orphaned[k] = orphaned[k] + 1
    xs = [k * width + width / 2.0 for k in range(n)]
    util = [100.0 * busy[k] / (slots * width) for k in range(n)]
    ox = [xs[k] for k in range(n) if done[k] > 0]
    oy = [100.0 * orphaned[k] / done[k] for k in range(n) if done[k] > 0]
    ux, uy = mf.bucket_users(users, t0, seconds, width)

    fig, axes = plt.subplots(3, 1, figsize=(ONE_COLUMN, 3.1), sharex=True)
    for ax in axes:
        shade_fault(ax, m)
        ax.set_ylim(-5, 108)
        ax.set_yticks([0, 50, 100])
    axes[0].plot(xs, util, "k-", marker="o", markevery=2)
    axes[0].set_ylabel("Slots busy\n(%)")
    axes[1].plot(ox, oy, "k-", marker="s", markevery=2)
    axes[1].set_ylabel("Orphaned\nwork (%)")
    axes[2].plot(ux, uy, "k-", marker="^", markevery=2)
    axes[2].set_ylabel("Users\nserved (%)")
    axes[2].set_xlabel("Time (s)")
    axes[2].set_xlim(0, seconds)
    fig.tight_layout(h_pad=0.3)
    fig.align_ylabels(axes)
    save(fig, "plot_busy.pdf")


def summary_plot():
    rows = mf.load_runs()
    if len(rows) == 0:
        print("  skipped summary plot")
        return
    arms = ["no_retry", "static_retry", "retry_budget", "reactive", "predictive"]
    names = ["No\nretry", "Static\nretry", "Retry\nbudget", "Reactive\n(ours)", "Predictive\n(ours)"]
    periods = [("normal_success_pct", "Healthy period", "white", ""),
               ("fault_success_pct", "During fault", "white", "////"),
               ("recovery_success_pct", "After fault removed", "0.45", "")]
    experiments = [("metastable", "(a) Metastable latency fault"), ("error_burst", "(b) Error burst")]

    fig, axes = plt.subplots(1, 2, figsize=(TWO_COLUMNS, 2.1), sharey=True)
    bar_w = 0.26
    for e in range(2):
        experiment, title = experiments[e]
        ax = axes[e]
        ax.grid(axis="x", visible=False)
        for p in range(len(periods)):
            key, label, colour, hatch = periods[p]
            xs = []
            means = []
            stds = []
            for k in range(len(arms)):
                values = [float(r[key]) for r in rows if r["experiment"] == experiment and r["arm"] == arms[k]]
                mean, std = mf.mean_std(values)
                xs.append(k + (p - 1) * bar_w)
                means.append(mean)
                stds.append(std)
            ax.bar(xs, means, width=bar_w, color=colour, edgecolor="black", hatch=hatch, linewidth=0.5,
                   yerr=stds, capsize=1.5, error_kw={"elinewidth": 0.5, "capthick": 0.5}, label=label, zorder=2)
        ax.set_xticks(range(len(arms)))
        ax.set_xticklabels(names)
        ax.set_ylim(0, 110)
        ax.set_xlabel(title)
    axes[0].set_ylabel("Users served (%)")
    handles, labels = axes[0].get_legend_handles_labels()
    fig.legend(handles, labels, loc="lower center", ncol=3, bbox_to_anchor=(0.5, 0.97))
    fig.tight_layout(w_pad=0.8)
    save(fig, "plot_summary.pdf")


def detection_plot(scenario_id="ds_06", source="gateway", dest="order_service"):
    import csv
    with open(os.path.join(DATA_DIR, "labeled_windows.csv"), "r") as f:
        rows = [r for r in csv.DictReader(f) if r["scenario_id"] == scenario_id
                and r["source_service"] == source and r["dest_service"] == dest]
    rows.sort(key=mf.row_index)
    m = mf.find_manifest(scenario_id)
    risk = {}
    with open(os.path.join(RESULTS_DIR, "prediction_oof.csv"), "r") as f:
        for r in csv.DictReader(f):
            if r["scenario_id"] == scenario_id and r["edge"] == source + "->" + dest:
                risk[int(r["window_index"])] = float(r["risk"])

    xs = [float(r["t_rel"]) + 1.0 for r in rows]
    warm = [r for r in rows if float(r["t_rel"]) < 8.0 and int(r["n_calls"]) >= 4]
    base_s = mf.median([float(r["s_useful"]) for r in warm])
    base_c = mf.median([float(r["concurrency"]) for r in warm])
    panels = [("rho", r"$\rho$", 1.0, r"$\rho=1$"),
              ("s_useful", r"$S$ (calls/s)", 0.5 * base_s, r"$0.5\,S^*$"),
              ("concurrency", r"$N$ (in flight)", max(2.0 * base_c, 1.0), r"$2N^*$")]

    fig, axes = plt.subplots(4, 1, figsize=(ONE_COLUMN, 3.7), sharex=True)
    start, end = mf.fault_span(m)
    for ax in axes:
        for r in rows:
            if r["label"] == "livelock":
                ax.axvspan(float(r["t_rel"]), float(r["t_rel"]) + 2.0, color="0.85", linewidth=0, zorder=0)
        ax.axvline(start - m["start"], color="black", linestyle=":", linewidth=0.8)
        ax.axvline(end - m["start"], color="black", linestyle=":", linewidth=0.8)
    for k in range(3):
        key, label, line, line_label = panels[k]
        axes[k].plot(xs, [float(r[key]) for r in rows], "k-", marker="o", markersize=2.2)
        axes[k].axhline(line, color="C3", linestyle="--", linewidth=0.8)
        axes[k].text(xs[-1], line, line_label, fontsize=6.5, color="C3", ha="right", va="bottom")
        axes[k].set_ylabel(label)
        axes[k].yaxis.set_major_locator(MaxNLocator(3))
    rx = []
    ry = []
    for r in rows:
        if int(r["window_index"]) in risk:
            rx.append(float(r["t_rel"]) + 1.0)
            ry.append(risk[int(r["window_index"])])
    axes[3].plot(rx, ry, "k-", marker="o", markersize=2.2)
    axes[3].axhline(mf.model_threshold(), color="C3", linestyle="--", linewidth=0.8)
    axes[3].set_ylim(-0.08, 1.08)
    axes[3].set_yticks([0, 0.5, 1])
    axes[3].set_ylabel("Risk $p$")
    axes[3].set_xlabel("Time (s)")
    axes[3].set_xlim(0, xs[-1] + 1)
    fig.tight_layout(h_pad=0.3)
    fig.align_ylabels(axes)
    save(fig, "plot_detection.pdf")


def lead_time_plot():
    import json
    with open(os.path.join(RESULTS_DIR, "prediction_metrics.json"), "r") as f:
        metrics = json.load(f)
    ours = metrics["early_warning"]["our model"]
    rule = metrics["early_warning"]["reactive rule"]
    bins = [2, 4, 6, 8, 10]
    fig, ax = plt.subplots(figsize=(ONE_COLUMN, 1.75))
    ax.grid(axis="x", visible=False)
    for offset, data, colour, hatch, label in [(-0.4, ours, "0.35", "", "Our model"),
                                               (0.4, rule, "white", "////", "Reactive rule")]:
        counts = [len([x for x in data["lead_times"] if abs(x - b) < 0.01]) for b in bins]
        ax.bar([b + offset for b in bins], counts, width=0.8, color=colour, edgecolor="black", hatch=hatch,
               linewidth=0.5, zorder=2,
               label="%s (%d of %d episodes)" % (label, len(data["lead_times"]), data["episodes"]))
    ax.set_xticks(bins)
    ax.yaxis.set_major_locator(MaxNLocator(integer=True))
    ax.set_xlabel("Warning before livelock started (s)")
    ax.set_ylabel("Episodes")
    ax.legend(loc="upper right")
    fig.tight_layout()
    save(fig, "plot_lead_time.pdf")


def evaluation_plot():
    rows = ev.load_rows()
    y = ev.column(rows, "y")
    curves = [("risk", "LR (ours)", "C0", "-"), ("p_forest", "Random forest", "C2", "-."),
              ("p_no_graph", "LR, no graph feat.", "C1", "--"), ("p_shuffled", "Shuffled labels", "0.5", ":")]
    fig, axes = plt.subplots(2, 2, figsize=(ONE_COLUMN, 3.3))

    ax = axes[0][0]
    for col, label, colour, dash in curves:
        fpr, tpr, cut = roc_curve(y, ev.column(rows, col))
        ax.plot(fpr, tpr, color=colour, linestyle=dash,
                label="%s %.2f" % (label, roc_auc_score(y, ev.column(rows, col))))
    ax.plot([0, 1], [0, 1], color="black", linewidth=0.5)
    ax.set_xlabel("False positive rate\n(a) ROC (AUC)")
    ax.set_ylabel("True positive rate")

    ax = axes[0][1]
    for col, label, colour, dash in curves:
        precision, recall, cut = precision_recall_curve(y, ev.column(rows, col))
        ax.plot(recall, precision, color=colour, linestyle=dash,
                label="%s %.2f" % (label, average_precision_score(y, ev.column(rows, col))))
    ax.set_ylim(0, 1.03)
    ax.set_xlabel("Recall\n(b) Precision-recall (AP)")
    ax.set_ylabel("Precision")

    ax = axes[1][0]
    for col, label, colour, marker in [("risk", "LR (ours)", "C0", "o"), ("p_forest", "Random forest", "C2", "^")]:
        calib = ev.calibration(y, ev.column(rows, col))
        table = calib["table"]
        ax.plot([b["mean_risk"] for b in table], [b["actual_rate"] for b in table], color=colour, marker=marker,
                label="%s, ECE %.2f" % (label, calib["ece"]))
    ax.plot([0, 1], [0, 1], color="black", linewidth=0.5)
    ax.set_xlabel("Predicted risk\n(c) Calibration")
    ax.set_ylabel("Observed rate")
    ax.legend(loc="upper left", fontsize=5.5, handlelength=1.5)

    ax = axes[1][1]
    sweep_rows = ev.sweep(rows)
    ts = [s["threshold"] for s in sweep_rows]
    ax.plot(ts, [s["precision"] for s in sweep_rows], "C2-.", marker="^", label="Precision")
    ax.plot(ts, [s["recall"] for s in sweep_rows], "C3--", marker="s", label="Recall")
    ax.plot(ts, [s["f1"] for s in sweep_rows], "C0-", marker="o", label="F1")
    ax.plot(ts, [s["healthy_alarm_pct"] / 100.0 for s in sweep_rows], "k:", marker="x", label="Healthy alarms")
    ax.axvline(mf.model_threshold(), color="black", linewidth=0.5)
    ax.set_ylim(0, 1.03)
    ax.set_xlabel("Threshold\n(d) Moving the threshold")
    ax.legend(loc="lower right", bbox_to_anchor=(1.0, 0.06), fontsize=5.5, handlelength=1.8)

    axes[0][0].legend(loc="lower right", fontsize=5.5, handlelength=1.8)
    axes[0][1].legend(loc="lower left", fontsize=5.5, handlelength=1.8)
    fig.tight_layout(h_pad=0.6, w_pad=0.6)
    save(fig, "plot_model_eval.pdf")


def controller_plot(experiment, rep):
    scenario_id = "exp_%s_predictive_r%d" % (experiment, rep)
    m = mf.find_manifest(scenario_id)
    records, skipped = read_jsonl(data_path(CONTROLLER_LOG_NAME))
    records = [r for r in records if r.get("tag") == scenario_id]
    if m is None or len(records) == 0:
        print("  skipped controller plot")
        return
    t0 = m["start"]
    fig, axes = plt.subplots(2, 1, figsize=(ONE_COLUMN, 2.6), sharex=True)
    for ax in axes:
        shade_fault(ax, m)
    for name, label, colour, dash, marker in EDGE_LINES:
        rows = [r for r in records if r["edge"] == name]
        xs = [r["timestamp"] - t0 for r in rows]
        axes[0].step(xs, [r["max_attempts"] for r in rows], where="post", color=colour, linestyle=dash,
                     label=label)
        rx = [r["timestamp"] - t0 for r in rows if r["risk"] is not None]
        ry = [r["risk"] for r in rows if r["risk"] is not None]
        axes[1].plot(rx, ry, color=colour, linestyle=dash, marker=marker, markevery=4, label=label)
    axes[0].set_yticks([1, 2, 3])
    axes[0].set_ylim(0.7, 3.3)
    axes[0].set_ylabel("Attempts allowed")
    axes[1].axhline(mf.model_threshold(), color="black", linewidth=0.5)
    axes[1].set_ylim(-0.08, 1.08)
    axes[1].set_ylabel("Risk $p$")
    axes[1].set_xlabel("Time (s)")
    axes[1].set_xlim(0, m["end"] - t0)
    handles, labels = axes[1].get_legend_handles_labels()
    fig.legend(handles, labels, loc="lower center", ncol=3, bbox_to_anchor=(0.55, 0.98),
               handlelength=2.0, columnspacing=0.8, fontsize=6.5)
    fig.tight_layout(h_pad=0.4)
    fig.align_ylabels(axes)
    save(fig, "plot_controller.pdf")


def main():
    print("drawing report plots...")
    timeline_plot("metastable", 1, "plot_timeline.pdf")
    busy_plot("metastable", 1)
    summary_plot()
    detection_plot()
    lead_time_plot()
    evaluation_plot()
    controller_plot("metastable", 1)


if __name__ == "__main__":
    main()
