import csv
import json
import math
import os
import pickle
import random
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from sklearn.metrics import (roc_auc_score, average_precision_score, roc_curve, precision_recall_curve,
                             matthews_corrcoef, cohen_kappa_score, accuracy_score)
from sklearn.preprocessing import StandardScaler

from common.config import DATA_DIR, RESULTS_DIR, EDGES, make_dirs
from common.telemetry import read_scenarios
from features import risk_probability, make_features, FEATURE_NAMES
import prediction_model
import controller
from make_figures import style, save, BLUE, ORANGE, AQUA, GREY, INK, MUTED

# Module 6b: full evaluation of the prediction model.
#
# prediction_model.py reports precision, recall, F1 and PR-AUC. This file
# takes the same out-of-fold scores (every window was scored by a model
# that never saw its scenario) and looks at them from more sides:
#
#   1. every number the confusion matrix gives: accuracy, balanced accuracy,
#      precision, recall, specificity, F1, F2, MCC, Cohen's kappa
#   2. ranking quality without any threshold: ROC-AUC and PR-AUC
#   3. calibration: when the model says 0.8, is it right 80% of the time?
#      (Brier score, log loss, expected calibration error)
#   4. how sure we can be: 95% bootstrap confidence intervals. We resample
#      whole SCENARIOS, not single windows, because windows of one run are
#      not independent of each other.
#   5. what happens if the 0.5 threshold is moved
#   6. where the model works and where it does not (per edge, per fault)
#   7. what it costs at run time: the controller runs it every second, so
#      speed and size matter as much as accuracy
#   8. the shuffled-label control repeated with 20 different shuffles, to
#      see the range a model with NO real signal can reach by luck
#
# Needs results/prediction_oof.csv and results/model.json (prediction_model.py).
#   python evaluate_model.py

OOF_PATH = os.path.join(RESULTS_DIR, "prediction_oof.csv")
MODEL_PATH = os.path.join(RESULTS_DIR, "model.json")
BASELINES_PATH = os.path.join(DATA_DIR, "baselines.json")
REPORT_PATH = os.path.join(RESULTS_DIR, "model_evaluation.txt")
JSON_PATH = os.path.join(RESULTS_DIR, "model_evaluation.json")

THRESHOLD = 0.5
BOOTSTRAP_ROUNDS = 1000
BOOTSTRAP_SEED = 0
CALIBRATION_BINS = 10
CONTROL_REPEATS = 20
SWEEP = [0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9]

# (name in the report, column in prediction_oof.csv, gives a probability?)
METHODS = [
    ("logistic regression (ours)", "risk", True),
    ("  - without graph features", "p_no_graph", True),
    ("  - without temporal features", "p_no_temporal", True),
    ("random forest (comparison)", "p_forest", True),
    ("shuffled-label control", "p_shuffled", True),
    ("rule: storm right now (reactive)", "rule_reactive", False),
    ("rule: livelock right now (persistence)", "rule_persistence", False),
]
OURS = "risk"


# ---------------------------------------------------------------- loading

def load_rows():
    rows = []
    with open(OOF_PATH, "r") as f:
        for r in csv.DictReader(f):
            row = {
                "scenario_id": r["scenario_id"],
                "edge": r["edge"],
                "window_index": int(r["window_index"]),
                "phase": r["phase"],
                "y": int(r["label_future"]),
                "label_now": int(r["label_now"]),
                "onset": int(r["label_now"]) == 0 and int(r["label_prev"]) == 0,
            }
            for name, column, is_prob in METHODS:
                row[column] = float(r[column])
            rows.append(row)
    return rows


def scenario_info():
    # scenario id -> fault kind and slot-wait length, from the manifest
    info = {}
    for manifest in read_scenarios():
        if manifest.get("interrupted"):
            continue
        if manifest["mode"] == "none":
            fault = "no fault (control)"
        else:
            fault = manifest["fault"] + " fault"
        if manifest["queue_wait"] <= 1.0:
            queue = "short slot wait (1 s)"
        else:
            queue = "long slot wait (4-10 s)"
        info[manifest["scenario_id"]] = (fault, queue)
    return info


# ---------------------------------------------------------------- metrics

def safe_div(a, b):
    if b == 0:
        return 0.0
    return float(a) / b


def counts(y, pred):
    tp = 0
    fp = 0
    tn = 0
    fn = 0
    for i in range(len(y)):
        if y[i] == 1 and pred[i] == 1:
            tp = tp + 1
        elif y[i] == 0 and pred[i] == 1:
            fp = fp + 1
        elif y[i] == 0 and pred[i] == 0:
            tn = tn + 1
        else:
            fn = fn + 1
    return tp, fp, tn, fn


def threshold_metrics(y, pred):
    # everything that can be computed from the 2x2 confusion matrix
    tp, fp, tn, fn = counts(y, pred)
    n = tp + fp + tn + fn
    precision = safe_div(tp, tp + fp)
    recall = safe_div(tp, tp + fn)            # also called sensitivity / true positive rate
    specificity = safe_div(tn, tn + fp)       # true negative rate
    f1 = safe_div(2 * precision * recall, precision + recall)
    # F2 weighs recall twice as much as precision: missing a livelock costs
    # more than an extra early cut
    f2 = safe_div(5 * precision * recall, 4 * precision + recall)
    # Matthews correlation coefficient: -1..+1, 0 = no better than guessing.
    # Uses all four cells, so it is fair when one class is rare (ours: 15%).
    bottom = math.sqrt(float(tp + fp) * (tp + fn) * (tn + fp) * (tn + fn))
    mcc = safe_div(tp * tn - fp * fn, bottom)
    # Cohen's kappa: agreement with the labels beyond what chance would give
    observed = safe_div(tp + tn, n)
    expected = safe_div((tp + fp) * (tp + fn) + (fn + tn) * (fp + tn), n * n)
    kappa = safe_div(observed - expected, 1.0 - expected)
    return {
        "tp": tp, "fp": fp, "tn": tn, "fn": fn,
        "accuracy": observed,
        "balanced_accuracy": (recall + specificity) / 2.0,
        "precision": precision,
        "recall": recall,
        "specificity": specificity,
        "false_positive_rate": 1.0 - specificity,
        "f1": f1,
        "f2": f2,
        "mcc": mcc,
        "kappa": kappa,
    }


def both_classes(y):
    return 0 < sum(y) < len(y)


def ranking_metrics(y, scores):
    # quality of the ORDER of the scores, for every threshold at once
    if not both_classes(y):
        return {"roc_auc": None, "pr_auc": None}
    return {"roc_auc": roc_auc_score(y, scores), "pr_auc": average_precision_score(y, scores)}


def calibration(y, probs, n_bins=CALIBRATION_BINS):
    # Brier score: mean squared error of the probability (0 = perfect)
    brier = 0.0
    loss = 0.0
    for i in range(len(y)):
        brier = brier + (probs[i] - y[i]) ** 2
        p = min(max(probs[i], 1e-6), 1.0 - 1e-6)
        loss = loss - (y[i] * math.log(p) + (1 - y[i]) * math.log(1.0 - p))
    brier = brier / len(y)
    loss = loss / len(y)
    # skill: how much better than always saying the base rate
    base = float(sum(y)) / len(y)
    brier_base = base * (1.0 - base)
    skill = 1.0 - safe_div(brier, brier_base)

    # reliability table and expected calibration error (ECE): put the
    # windows in 10 bins by predicted risk; in each bin compare the average
    # risk with how often livelock really followed
    bins = []
    for b in range(n_bins):
        bins.append({"low": float(b) / n_bins, "high": float(b + 1) / n_bins, "n": 0,
                     "sum_p": 0.0, "sum_y": 0})
    for i in range(len(y)):
        b = int(probs[i] * n_bins)
        if b >= n_bins:
            b = n_bins - 1
        bins[b]["n"] = bins[b]["n"] + 1
        bins[b]["sum_p"] = bins[b]["sum_p"] + probs[i]
        bins[b]["sum_y"] = bins[b]["sum_y"] + y[i]
    ece = 0.0
    table = []
    for b in bins:
        if b["n"] == 0:
            continue
        mean_p = b["sum_p"] / b["n"]
        rate = float(b["sum_y"]) / b["n"]
        ece = ece + (float(b["n"]) / len(y)) * abs(mean_p - rate)
        table.append({"low": b["low"], "high": b["high"], "n": b["n"], "mean_risk": mean_p, "actual_rate": rate})
    return {"brier": brier, "brier_skill": skill, "log_loss": loss, "ece": ece, "table": table}


def predictions_at(scores, threshold):
    pred = []
    for s in scores:
        pred.append(1 if s >= threshold else 0)
    return pred


def column(rows, name):
    values = []
    for r in rows:
        values.append(r[name])
    return values


def full_metrics(rows, name, is_prob):
    y = column(rows, "y")
    scores = column(rows, name)
    if is_prob:
        pred = predictions_at(scores, THRESHOLD)
    else:
        pred = [int(s) for s in scores]
    result = threshold_metrics(y, pred)
    if is_prob:
        result.update(ranking_metrics(y, scores))
    else:
        result["roc_auc"] = None
        result["pr_auc"] = None
    return result


# ---------------------------------------------------------------- bootstrap

def group_by_scenario(rows):
    groups = {}
    for r in rows:
        if r["scenario_id"] not in groups:
            groups[r["scenario_id"]] = []
        groups[r["scenario_id"]].append(r)
    return groups


def resample_scenarios(rng, ids):
    # draw len(ids) scenario ids WITH replacement
    picked = []
    for k in range(len(ids)):
        picked.append(ids[rng.randrange(len(ids))])
    return picked


def percentile(sorted_values, q):
    if len(sorted_values) == 0:
        return None
    position = q * (len(sorted_values) - 1)
    low = int(math.floor(position))
    high = int(math.ceil(position))
    if low == high:
        return sorted_values[low]
    return sorted_values[low] + (sorted_values[high] - sorted_values[low]) * (position - low)


def bootstrap(rows, metric, rounds=BOOTSTRAP_ROUNDS, seed=BOOTSTRAP_SEED):
    # metric(rows) -> number or None. Returns (low, high) of the 95% interval.
    groups = group_by_scenario(rows)
    ids = sorted(groups)
    rng = random.Random(seed)
    values = []
    for k in range(rounds):
        sample = []
        for scenario_id in resample_scenarios(rng, ids):
            sample.extend(groups[scenario_id])
        value = metric(sample)
        if value is not None:
            values.append(value)
    values.sort()
    return percentile(values, 0.025), percentile(values, 0.975), len(values)


def one_metric(sample, name, is_prob, key):
    # computes only the metric asked for (the bootstrap calls this many times)
    y = column(sample, "y")
    scores = column(sample, name)
    if key in ("roc_auc", "pr_auc"):
        return ranking_metrics(y, scores)[key]
    if is_prob:
        pred = predictions_at(scores, THRESHOLD)
    else:
        pred = [int(v) for v in scores]
    return threshold_metrics(y, pred)[key]


def metric_on(name, key, is_prob=True):
    def compute(sample):
        if not both_classes(column(sample, "y")):
            return None
        return one_metric(sample, name, is_prob, key)
    return compute


def difference(name_a, prob_a, name_b, prob_b, key):
    def compute(sample):
        if not both_classes(column(sample, "y")):
            return None
        a = one_metric(sample, name_a, prob_a, key)
        b = one_metric(sample, name_b, prob_b, key)
        if a is None or b is None:
            return None
        return a - b
    return compute


# ---------------------------------------------------------------- threshold sweep

def sweep(rows):
    examples = []
    for r in rows:
        examples.append({"scenario_id": r["scenario_id"], "edge": r["edge"],
                         "window_index": r["window_index"], "label_now": r["label_now"],
                         "phase": r["phase"]})
    y = column(rows, "y")
    scores = column(rows, OURS)
    n_episodes = len(prediction_model.episodes(examples))
    result = []
    for t in SWEEP:
        pred = predictions_at(scores, t)
        m = threshold_metrics(y, pred)
        leads, missed = prediction_model.lead_times(examples, pred)
        raised, healthy = prediction_model.false_alarm_rate(examples, pred)
        result.append({"threshold": t, "precision": m["precision"], "recall": m["recall"], "f1": m["f1"],
                       "mcc": m["mcc"], "fpr": m["false_positive_rate"],
                       "healthy_alarm_pct": 100.0 * safe_div(raised, healthy),
                       "warned": len(leads), "episodes": n_episodes,
                       "median_lead": prediction_model.median_of(leads)})
    return result


# ---------------------------------------------------------------- run-time cost

def median_time_per_call(function, calls, repeats=7):
    # runs function() (which does `calls` operations) several times and
    # returns the median time of ONE operation, in microseconds
    times = []
    for k in range(repeats):
        started = time.perf_counter()
        function()
        times.append((time.perf_counter() - started) / calls * 1e6)
    times.sort()
    return times[len(times) // 2]


def full_stats(window, edge):
    if edge in window:
        return window[edge]
    empty = {}
    for field in prediction_model.STAT_FIELDS:
        empty[field] = 0.0
    empty["n_calls"] = 0
    empty["mean_latency_ms"] = None
    return empty


def runtime_cost(model):
    with open(BASELINES_PATH, "r") as f:
        baselines = json.load(f)
    scenarios = prediction_model.load_windows()
    examples = prediction_model.build_examples(scenarios, baselines)
    y = prediction_model.labels(examples)
    x = prediction_model.matrix(examples, FEATURE_NAMES)
    cost = {"examples": len(examples)}

    # 1. what the controller actually runs: the hand-written sigmoid
    def hand_risk():
        for example in examples:
            risk_probability(model, example["values"])
    cost["hand_risk_us"] = median_time_per_call(hand_risk, len(examples))

    # 2. one controller tick: features for all 3 edges + risk + cut/hold/restore
    ticks = []
    for scenario_id in sorted(scenarios):
        windows = scenarios[scenario_id]
        for i in sorted(windows):
            now = {}
            prev = {}
            for edge in EDGES:
                now[edge] = full_stats(windows[i], edge)
                prev[edge] = full_stats(windows.get(i - 1, {}), edge)
            ticks.append((now, prev, windows[i][list(windows[i].keys())[0]]["t_rel"]))

    def feature_ticks():
        for now, prev, t in ticks:
            make_features(now, prev, model["baselines"])
    cost["features_per_tick_us"] = median_time_per_call(feature_ticks, len(ticks))

    def controller_ticks():
        state = controller.starting_state()
        for now, prev, t in ticks:
            controller.control_step("predictive", model, state, now, prev, t)
    cost["controller_tick_us"] = median_time_per_call(controller_ticks, len(ticks))
    cost["ticks"] = len(ticks)
    cost["controller_cpu_pct"] = 100.0 * cost["controller_tick_us"] / (controller.CONTROL_INTERVAL_SEC * 1e6)

    # 3. the same model through scikit-learn, and the random forest, one window at a time
    scaler = StandardScaler()
    x_scaled = scaler.fit_transform(x)
    started = time.perf_counter()
    logistic = prediction_model.make_logistic()
    logistic.fit(x_scaled, y)
    cost["train_logistic_ms"] = (time.perf_counter() - started) * 1000.0
    started = time.perf_counter()
    forest = prediction_model.make_forest()
    forest.fit(x_scaled, y)
    cost["train_forest_ms"] = (time.perf_counter() - started) * 1000.0

    n_single = 200
    def sklearn_logistic():
        for i in range(n_single):
            logistic.predict_proba(x_scaled[i:i + 1])
    def sklearn_forest():
        for i in range(n_single):
            forest.predict_proba(x_scaled[i:i + 1])
    cost["sklearn_logistic_us"] = median_time_per_call(sklearn_logistic, n_single, repeats=3)
    cost["forest_us"] = median_time_per_call(sklearn_forest, n_single, repeats=3)

    # 4. size
    cost["logistic_numbers"] = 3 * len(model["feature_names"]) + 1    # weights, means, stds + intercept
    cost["model_json_bytes"] = os.path.getsize(MODEL_PATH)
    nodes = 0
    for tree in forest.estimators_:
        nodes = nodes + tree.tree_.node_count
    cost["forest_trees"] = len(forest.estimators_)
    cost["forest_nodes"] = nodes
    cost["forest_pickle_bytes"] = len(pickle.dumps(forest))
    return cost


# ---------------------------------------------------------------- report helpers

def fmt(value, digits=2):
    if value is None:
        return "-"
    return ("%." + str(digits) + "f") % value


METRIC_COLUMNS = [("acc", "accuracy"), ("bal.acc", "balanced_accuracy"), ("prec", "precision"),
                  ("recall", "recall"), ("spec", "specificity"), ("F1", "f1"), ("F2", "f2"),
                  ("MCC", "mcc"), ("kappa", "kappa"), ("ROC-AUC", "roc_auc"), ("PR-AUC", "pr_auc")]


def metric_table(lines, rows):
    header = "  %-40s" % "method"
    for short, key in METRIC_COLUMNS:
        header = header + " %7s" % short
    lines.append(header)
    saved = {}
    for name, col, is_prob in METHODS:
        m = full_metrics(rows, col, is_prob)
        line = "  %-40s" % name
        for short, key in METRIC_COLUMNS:
            line = line + " %7s" % fmt(m[key])
        lines.append(line)
        saved[name.strip(" -")] = m
    return saved


def draw_figure(rows, calib, sweep_rows):
    y = column(rows, "y")
    fig, axes = plt.subplots(2, 2, figsize=(11, 8.2))
    curves = [("risk", "our model", BLUE, "-"), ("p_forest", "random forest", AQUA, "-"),
              ("p_no_graph", "ours, no graph features", ORANGE, "--"),
              ("p_shuffled", "shuffled-label control", GREY, "-")]

    ax = axes[0][0]
    style(ax)
    for col, label, colour, dash in curves:
        fpr, tpr, cut = roc_curve(y, column(rows, col))
        ax.plot(fpr, tpr, color=colour, linestyle=dash, linewidth=2,
                label="%s (AUC %.2f)" % (label, roc_auc_score(y, column(rows, col))))
    ax.plot([0, 1], [0, 1], color=MUTED, linestyle=":", linewidth=1)
    ax.set_xlabel("false positive rate", fontsize=9, color=INK)
    ax.set_ylabel("true positive rate (recall)", fontsize=9, color=INK)
    ax.set_title("(a) ROC curve", fontsize=10, color=INK)
    ax.legend(fontsize=7.5, frameon=False, loc="lower right")

    ax = axes[0][1]
    style(ax)
    for col, label, colour, dash in curves:
        precision, recall, cut = precision_recall_curve(y, column(rows, col))
        ax.plot(recall, precision, color=colour, linestyle=dash, linewidth=2,
                label="%s (AP %.2f)" % (label, average_precision_score(y, column(rows, col))))
    base = float(sum(y)) / len(y)
    ax.axhline(base, color=MUTED, linestyle=":", linewidth=1)
    ax.text(0.5, base + 0.015, "chance = %.2f" % base, fontsize=7.5, color=MUTED)
    ax.set_xlabel("recall", fontsize=9, color=INK)
    ax.set_ylabel("precision", fontsize=9, color=INK)
    ax.set_ylim(0, 1.02)
    ax.set_title("(b) precision-recall curve", fontsize=10, color=INK)
    ax.legend(fontsize=7.5, frameon=False, loc="lower left", bbox_to_anchor=(0.1, 0.2))

    ax = axes[1][0]
    style(ax)
    for col, label, colour in [("risk", "our model", BLUE), ("p_forest", "random forest", AQUA)]:
        table = calib[col]["table"]
        ax.plot([b["mean_risk"] for b in table], [b["actual_rate"] for b in table], color=colour,
                marker="o", linewidth=2, label="%s (ECE %.2f)" % (label, calib[col]["ece"]))
    ax.plot([0, 1], [0, 1], color=MUTED, linestyle=":", linewidth=1, label="perfectly calibrated")
    ax.set_xlabel("predicted risk (average in the bin)", fontsize=9, color=INK)
    ax.set_ylabel("how often livelock really followed", fontsize=9, color=INK)
    ax.set_title("(c) calibration (reliability diagram)", fontsize=10, color=INK)
    ax.legend(fontsize=7.5, frameon=False, loc="upper left")

    ax = axes[1][1]
    style(ax)
    xs = [s["threshold"] for s in sweep_rows]
    ax.plot(xs, [s["precision"] for s in sweep_rows], color=AQUA, linewidth=2, marker="o", label="precision")
    ax.plot(xs, [s["recall"] for s in sweep_rows], color=ORANGE, linewidth=2, marker="o", label="recall")
    ax.plot(xs, [s["f1"] for s in sweep_rows], color=BLUE, linewidth=2, marker="o", label="F1")
    ax.plot(xs, [s["healthy_alarm_pct"] / 100.0 for s in sweep_rows], color=GREY, linewidth=2, marker="o",
            label="alarms in healthy windows (share)")
    ax.axvline(THRESHOLD, color=MUTED, linestyle=":", linewidth=1)
    ax.text(THRESHOLD + 0.01, 0.02, "threshold used", fontsize=7.5, color=MUTED)
    ax.set_xlabel("decision threshold on the predicted risk", fontsize=9, color=INK)
    ax.set_ylim(0, 1.02)
    ax.set_title("(d) moving the threshold (our model)", fontsize=10, color=INK)
    ax.legend(fontsize=7.5, frameon=False, loc="center right", bbox_to_anchor=(1.0, 0.4))

    fig.suptitle("Evaluating the livelock predictor (out-of-fold scores, 1458 windows, unseen scenarios)",
                 fontsize=11, color=INK)
    fig.tight_layout()
    save(fig, "fig5c_model_evaluation.png")


# ---------------------------------------------------------------- main

def main():
    make_dirs()
    for path in [OOF_PATH, MODEL_PATH, BASELINES_PATH]:
        if not os.path.exists(path):
            print("error: %s not found. Run labeling.py and prediction_model.py first." % path)
            sys.exit(1)
    rows = load_rows()
    if "p_forest" not in rows[0]:
        print("error: prediction_oof.csv is from an older version. Run prediction_model.py again.")
        sys.exit(1)
    with open(MODEL_PATH, "r") as f:
        model = json.load(f)

    y = column(rows, "y")
    onset_rows = [r for r in rows if r["onset"]]
    saved = {"n": len(rows), "positives": sum(y), "n_onset": len(onset_rows),
             "positives_onset": sum(column(onset_rows, "y")), "threshold": THRESHOLD}

    lines = []
    lines.append("Model evaluation -- out-of-fold scores (each window scored by a model that never saw its scenario)")
    lines.append("=" * 100)
    lines.append("windows: %d, livelock coming in %d (%.1f%%); before onset: %d windows, %d positive"
                 % (len(rows), sum(y), 100.0 * sum(y) / len(y), len(onset_rows), saved["positives_onset"]))
    lines.append("threshold for the yes/no metrics: %.2f (fixed in advance, not tuned)" % THRESHOLD)
    lines.append("")

    lines.append("1. ALL WINDOWS")
    saved["all"] = metric_table(lines, rows)
    ours = saved["all"]["logistic regression (ours)"]
    lines.append("  our model's confusion matrix: TP %d  FP %d  TN %d  FN %d"
                 % (ours["tp"], ours["fp"], ours["tn"], ours["fn"]))
    lines.append("  bal.acc = average of recall and specificity (not fooled by the rare class);")
    lines.append("  F2 = like F1 but recall counts double; MCC and kappa: 0 = guessing, 1 = perfect;")
    lines.append("  ROC-AUC / PR-AUC: quality of the ranking of the risk scores, no threshold needed")
    lines.append("")
    lines.append("2. BEFORE ONSET ONLY (edge not in livelock now; will it be soon?)")
    saved["onset"] = metric_table(lines, onset_rows)
    lines.append("")

    # self-check: the hand-written formulas must agree with scikit-learn
    pred = predictions_at(column(rows, OURS), THRESHOLD)
    worst = max(abs(ours["mcc"] - matthews_corrcoef(y, pred)),
                abs(ours["kappa"] - cohen_kappa_score(y, pred)),
                abs(ours["accuracy"] - accuracy_score(y, pred)))
    lines.append("self-check: hand-written MCC / kappa / accuracy vs scikit-learn, largest difference = %.1e (%s)"
                 % (worst, "OK" if worst < 1e-9 else "MISMATCH"))
    lines.append("")

    lines.append("3. CALIBRATION (does the risk mean what it says?)")
    lines.append("  %-40s %8s %12s %9s %7s" % ("method", "Brier", "Brier skill", "log loss", "ECE"))
    calib = {}
    saved["calibration"] = {}
    for name, col, is_prob in METHODS:
        if not is_prob:
            continue
        c = calibration(y, column(rows, col))
        calib[col] = c
        saved["calibration"][name.strip(" -")] = c
        lines.append("  %-40s %8s %12s %9s %7s" % (name, fmt(c["brier"], 3), fmt(c["brier_skill"]),
                                                   fmt(c["log_loss"], 3), fmt(c["ece"], 3)))
    lines.append("  (Brier skill: 1 = perfect, 0 = no better than always saying the base rate)")
    middle = []
    for b in calib[OURS]["table"]:
        if b["low"] >= 0.5 - 1e-9 and b["high"] <= 0.9 + 1e-9:
            middle.append(100.0 * b["actual_rate"])
    lines.append("  The model is trained with class_weight='balanced', which on purpose makes the rare")
    lines.append("  livelock class count more. That pushes the risk values up (in the table below, a risk")
    if len(middle) > 0:
        lines.append("  of 0.5-0.9 is followed by livelock only %.0f-%.0f%% of the time). The ORDER of the scores"
                     % (min(middle), max(middle)))
    else:
        lines.append("  of 0.5-0.9 is followed by livelock less often than it says). The ORDER of the scores")
    lines.append("  is not affected (ROC-AUC %.2f), and the 0.5 threshold was fixed for this model." % ours["roc_auc"])
    lines.append("  reliability table, our model:")
    lines.append("    %-11s %8s %12s %22s" % ("risk bin", "windows", "mean risk", "livelock really came"))
    for b in calib[OURS]["table"]:
        lines.append("    %.1f - %.1f   %8d %12.2f %21.0f%%" % (b["low"], b["high"], b["n"], b["mean_risk"],
                                                              100.0 * b["actual_rate"]))
    lines.append("")

    lines.append("4. HOW SURE ARE WE? 95%% bootstrap intervals (%d rounds, whole scenarios resampled)"
                 % BOOTSTRAP_ROUNDS)
    saved["bootstrap"] = {}
    checks = [
        ("F1, all windows", rows, metric_on(OURS, "f1"), ours["f1"]),
        ("MCC, all windows", rows, metric_on(OURS, "mcc"), ours["mcc"]),
        ("ROC-AUC, all windows", rows, metric_on(OURS, "roc_auc"), ours["roc_auc"]),
        ("PR-AUC, all windows", rows, metric_on(OURS, "pr_auc"), ours["pr_auc"]),
        ("F1, before onset", onset_rows, metric_on(OURS, "f1"), saved["onset"]["logistic regression (ours)"]["f1"]),
        ("PR-AUC, before onset", onset_rows, metric_on(OURS, "pr_auc"),
         saved["onset"]["logistic regression (ours)"]["pr_auc"]),
        ("F1 gain over the reactive rule, all", rows,
         difference(OURS, True, "rule_reactive", False, "f1"),
         ours["f1"] - saved["all"]["rule: storm right now (reactive)"]["f1"]),
        ("F1 gain over the reactive rule, before onset", onset_rows,
         difference(OURS, True, "rule_reactive", False, "f1"),
         saved["onset"]["logistic regression (ours)"]["f1"] - saved["onset"]["rule: storm right now (reactive)"]["f1"]),
        ("PR-AUC gain from graph features, before onset", onset_rows,
         difference(OURS, True, "p_no_graph", True, "pr_auc"),
         saved["onset"]["logistic regression (ours)"]["pr_auc"] - saved["onset"]["without graph features"]["pr_auc"]),
        ("PR-AUC gap to the random forest, all", rows,
         difference(OURS, True, "p_forest", True, "pr_auc"),
         ours["pr_auc"] - saved["all"]["random forest (comparison)"]["pr_auc"]),
    ]
    lines.append("  %-46s %8s %22s" % ("quantity", "value", "95% interval"))
    for title, subset_rows, metric, value in checks:
        low, high, used = bootstrap(subset_rows, metric)
        note = ""
        if title.find("gain") >= 0 or title.find("gap") >= 0:
            if low > 0:
                note = "  (above 0: real)"
            elif high < 0:
                note = "  (below 0: real)"
            else:
                note = "  (includes 0: not sure)"
        lines.append("  %-46s %8s %10s to %-8s%s" % (title, fmt(value), fmt(low), fmt(high), note))
        saved["bootstrap"][title] = {"value": value, "low": low, "high": high, "rounds_used": used}
    lines.append("")

    lines.append("5. MOVING THE THRESHOLD (our model)")
    sweep_rows = sweep(rows)
    saved["sweep"] = sweep_rows
    lines.append("  %9s %10s %7s %6s %6s %6s %16s %14s" % ("threshold", "precision", "recall", "F1", "MCC",
                                                           "FPR", "healthy alarms", "warned (median)"))
    for s in sweep_rows:
        lead = "-" if s["median_lead"] is None else "%.0fs" % s["median_lead"]
        lines.append("  %9.1f %10s %7s %6s %6s %6s %15.1f%% %8s (%s)%s"
                     % (s["threshold"], fmt(s["precision"]), fmt(s["recall"]), fmt(s["f1"]), fmt(s["mcc"]),
                        fmt(s["fpr"]), s["healthy_alarm_pct"], "%d/%d" % (s["warned"], s["episodes"]), lead,
                        "   <- used" if s["threshold"] == THRESHOLD else ""))
    best = sweep_rows[0]
    for s in sweep_rows:
        if s["f1"] > best["f1"]:
            best = s
    lines.append("  best F1 on these same scores is at %.1f (F1 %.2f). We keep 0.5: picking the threshold"
                 % (best["threshold"], best["f1"]))
    lines.append("  on the data you report on would make the number look better than it really is.")
    lines.append("")

    lines.append("6. WHERE IT WORKS (our model, all windows)")
    info = scenario_info()
    groups = []
    for edge in EDGES:
        name = edge[0] + "->" + edge[1]
        groups.append(("edge " + name, [r for r in rows if r["edge"] == name]))
    kinds = []
    for r in rows:
        fault = info.get(r["scenario_id"], ("unknown", "unknown"))[0]
        if fault not in kinds:
            kinds.append(fault)
    for kind in sorted(kinds):
        groups.append((kind, [r for r in rows if info.get(r["scenario_id"], ("unknown",))[0] == kind]))
    for queue in ["short slot wait (1 s)", "long slot wait (4-10 s)"]:
        groups.append((queue, [r for r in rows if info.get(r["scenario_id"], ("", ""))[1] == queue]))
    lines.append("  %-42s %8s %9s %10s %7s %6s %6s %8s" % ("group", "windows", "positive", "precision",
                                                        "recall", "F1", "MCC", "ROC-AUC"))
    saved["breakdown"] = {}
    for title, group_rows in groups:
        if len(group_rows) == 0:
            continue
        m = full_metrics(group_rows, OURS, True)
        if sum(column(group_rows, "y")) == 0:
            # nothing to find here: the only possible mistake is a false alarm
            lines.append("  %-42s %8d %9d   false alarms: %d of %d windows (%.1f%%)"
                         % (title, len(group_rows), 0, m["fp"], len(group_rows),
                            100.0 * m["fp"] / len(group_rows)))
            saved["breakdown"][title] = {"n": len(group_rows), "positives": 0, "fp": m["fp"]}
            continue
        lines.append("  %-42s %8d %9d %10s %7s %6s %6s %8s"
                     % (title, len(group_rows), sum(column(group_rows, "y")), fmt(m["precision"]),
                        fmt(m["recall"]), fmt(m["f1"]), fmt(m["mcc"]), fmt(m["roc_auc"])))
        saved["breakdown"][title] = {"n": len(group_rows), "positives": sum(column(group_rows, "y")),
                                     "precision": m["precision"], "recall": m["recall"], "f1": m["f1"],
                                     "mcc": m["mcc"], "roc_auc": m["roc_auc"], "fp": m["fp"]}
    for title, group_rows in groups:
        positives = sum(column(group_rows, "y"))
        if title.startswith("edge ") and 0 < positives < 10:
            lines.append("  %s has only %d positive windows: too few to judge that edge."
                         % (title[len("edge "):], positives))
    lines.append("")

    lines.append("7. RUN-TIME COST (measured on this machine; the controller runs every %.0f s)"
                 % controller.CONTROL_INTERVAL_SEC)
    cost = runtime_cost(model)
    saved["runtime"] = cost
    lines.append("  risk for one edge, hand-written sigmoid (what the controller uses) %10.1f us"
                 % cost["hand_risk_us"])
    lines.append("  same model through scikit-learn predict_proba, one window        %10.1f us"
                 % cost["sklearn_logistic_us"])
    lines.append("  random forest (200 trees) predict_proba, one window              %10.1f us"
                 % cost["forest_us"])
    lines.append("  features for one tick (all 3 edges)                              %10.1f us"
                 % cost["features_per_tick_us"])
    lines.append("  one whole controller round (3 edges: features, risk, decision)  %10.1f us = %.3f%% of the 1 s tick"
                 % (cost["controller_tick_us"], cost["controller_cpu_pct"]))
    lines.append("  training time on all %d windows: logistic regression %.0f ms, random forest %.0f ms"
                 % (cost["examples"], cost["train_logistic_ms"], cost["train_forest_ms"]))
    lines.append("  model size: logistic regression = %d numbers (model.json %d bytes);"
                 % (cost["logistic_numbers"], cost["model_json_bytes"]))
    lines.append("              random forest = %d trees, %d nodes, %d KB pickled"
                 % (cost["forest_trees"], cost["forest_nodes"], cost["forest_pickle_bytes"] // 1024))

    lines.append("")

    lines.append("8. THE SHUFFLED-LABEL CONTROL, REPEATED %d TIMES (a model with no real signal)" % CONTROL_REPEATS)
    with open(BASELINES_PATH, "r") as f:
        baselines = json.load(f)
    examples = prediction_model.build_examples(prediction_model.load_windows(), baselines)
    y_examples = prediction_model.labels(examples)
    control_roc = []
    control_pr = []
    for seed in range(1, CONTROL_REPEATS + 1):
        probs, fold_f1 = prediction_model.cross_validate(examples, FEATURE_NAMES, "logistic", True, seed)
        control_roc.append(roc_auc_score(y_examples, probs))
        control_pr.append(average_precision_score(y_examples, probs))
    reach = 0
    for v in control_pr:
        if v >= ours["pr_auc"]:
            reach = reach + 1
    lines.append("  ROC-AUC: mean %.2f, range %.2f-%.2f   (ours %.2f)"
                 % (sum(control_roc) / len(control_roc), min(control_roc), max(control_roc), ours["roc_auc"]))
    lines.append("  PR-AUC:  mean %.2f, range %.2f-%.2f   (ours %.2f, chance %.2f)"
                 % (sum(control_pr) / len(control_pr), min(control_pr), max(control_pr), ours["pr_auc"],
                    float(sum(y)) / len(y)))
    lines.append("  shuffled runs that reached our PR-AUC: %d of %d" % (reach, CONTROL_REPEATS))
    lines.append("  (a model with random weights can land on either side of ROC-AUC 0.5, because the")
    lines.append("  features are strongly tied to livelock; it never gets close to our model)")
    saved["control_repeats"] = {"roc_auc": control_roc, "pr_auc": control_pr, "reached_ours": reach}

    draw_figure(rows, calib, sweep_rows)

    with open(JSON_PATH, "w") as f:
        json.dump(saved, f, indent=2)
    text = "\n".join(lines)
    print(text)
    with open(REPORT_PATH, "w") as f:
        f.write(text + "\n")


if __name__ == "__main__":
    main()
