import argparse
import csv
import json
import math
import os
import pickle
import random
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from sklearn.metrics import (roc_auc_score, average_precision_score, roc_curve, precision_recall_curve,
                             matthews_corrcoef, cohen_kappa_score, accuracy_score)
from sklearn.preprocessing import StandardScaler

from common.config import DATA_DIR, RESULTS_DIR, EDGES, make_dirs
from common.telemetry import read_scenarios
from features import risk_probability, make_features, FEATURE_NAMES
import prediction_model
import controller
from common.plots import plt, save_figure, BLUE, ORANGE, GREEN, RED, GREY

OOF_PATH = os.path.join(RESULTS_DIR, "prediction_oof.csv")
MODEL_PATH = os.path.join(RESULTS_DIR, "model.json")
BASELINES_PATH = os.path.join(DATA_DIR, "baselines.json")
REPORT_PATH = os.path.join(RESULTS_DIR, "model_evaluation.txt")
JSON_PATH = os.path.join(RESULTS_DIR, "model_evaluation.json")

BOOTSTRAP_ROUNDS = 1000
BOOTSTRAP_SEED = 0
CALIBRATION_BINS = 10
CONTROL_REPEATS = 20
SWEEP = [0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.65, 0.7, 0.8, 0.9]

METHODS = [
    ("Logistic regression", "risk", "alarm"),
    ("  w/o graph features", "p_no_graph", "a_no_graph"),
    ("  w/o temporal features", "p_no_temporal", "a_no_temporal"),
    ("Random forest", "p_forest", "a_forest"),
    ("Shuffled labels", "p_shuffled", "a_shuffled"),
    ("Reactive rule", None, "rule_reactive"),
    ("Persistence rule", None, "rule_persistence"),
]
OURS = "risk"
OURS_ALARM = "alarm"


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
            for name, prob_col, alarm_col in METHODS:
                if prob_col is not None:
                    row[prob_col] = float(r[prob_col])
                row[alarm_col] = int(r[alarm_col])
            rows.append(row)
    return rows


def scenario_info():
    info = {}
    for manifest in read_scenarios():
        if manifest.get("interrupted"):
            continue
        if manifest["mode"] == "none":
            fault = "No fault"
        else:
            fault = manifest["fault"].capitalize() + " fault"
        if manifest["queue_wait"] <= 1.0:
            queue = "Slot wait 1 s"
        else:
            queue = "Slot wait 4-10 s"
        info[manifest["scenario_id"]] = (fault, queue)
    return info


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
    tp, fp, tn, fn = counts(y, pred)
    n = tp + fp + tn + fn
    precision = safe_div(tp, tp + fp)
    recall = safe_div(tp, tp + fn)
    specificity = safe_div(tn, tn + fp)
    f1 = safe_div(2 * precision * recall, precision + recall)
    f2 = safe_div(5 * precision * recall, 4 * precision + recall)
    bottom = math.sqrt(float(tp + fp) * (tp + fn) * (tn + fp) * (tn + fn))
    mcc = safe_div(tp * tn - fp * fn, bottom)
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
    if not both_classes(y):
        return {"roc_auc": None, "pr_auc": None}
    return {"roc_auc": roc_auc_score(y, scores), "pr_auc": average_precision_score(y, scores)}


def calibration(y, probs, n_bins=CALIBRATION_BINS):
    brier = 0.0
    loss = 0.0
    for i in range(len(y)):
        brier = brier + (probs[i] - y[i]) ** 2
        p = min(max(probs[i], 1e-6), 1.0 - 1e-6)
        loss = loss - (y[i] * math.log(p) + (1 - y[i]) * math.log(1.0 - p))
    brier = brier / len(y)
    loss = loss / len(y)
    base = float(sum(y)) / len(y)
    brier_base = base * (1.0 - base)
    skill = 1.0 - safe_div(brier, brier_base)

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


def full_metrics(rows, prob_col, alarm_col):
    y = column(rows, "y")
    result = threshold_metrics(y, column(rows, alarm_col))
    if prob_col is not None:
        result.update(ranking_metrics(y, column(rows, prob_col)))
    else:
        result["roc_auc"] = None
        result["pr_auc"] = None
    return result


def group_by_scenario(rows):
    groups = {}
    for r in rows:
        if r["scenario_id"] not in groups:
            groups[r["scenario_id"]] = []
        groups[r["scenario_id"]].append(r)
    return groups


def resample_scenarios(rng, ids):
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


def one_metric(sample, prob_col, alarm_col, key):
    y = column(sample, "y")
    if key in ("roc_auc", "pr_auc"):
        return ranking_metrics(y, column(sample, prob_col))[key]
    return threshold_metrics(y, column(sample, alarm_col))[key]


def metric_on(prob_col, alarm_col, key):
    def compute(sample):
        if not both_classes(column(sample, "y")):
            return None
        return one_metric(sample, prob_col, alarm_col, key)
    return compute


def difference(a, b, key):
    def compute(sample):
        if not both_classes(column(sample, "y")):
            return None
        first = one_metric(sample, a[0], a[1], key)
        second = one_metric(sample, b[0], b[1], key)
        if first is None or second is None:
            return None
        return first - second
    return compute


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
        result.append({"threshold": t, "accuracy": m["accuracy"], "precision": m["precision"],
                       "recall": m["recall"], "f1": m["f1"], "mcc": m["mcc"], "fpr": m["false_positive_rate"],
                       "healthy_alarm_pct": 100.0 * safe_div(raised, healthy),
                       "warned": len(leads), "episodes": n_episodes,
                       "median_lead": prediction_model.median_of(leads)})
    return result


def median_time_per_call(function, calls, repeats=7):
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

    def hand_risk():
        for example in examples:
            risk_probability(model, example["values"])
    cost["hand_risk_us"] = median_time_per_call(hand_risk, len(examples))

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
    forest.set_params(n_jobs=1)

    n_single = 200
    def sklearn_logistic():
        for i in range(n_single):
            logistic.predict_proba(x_scaled[i:i + 1])
    def sklearn_forest():
        for i in range(n_single):
            forest.predict_proba(x_scaled[i:i + 1])
    cost["sklearn_logistic_us"] = median_time_per_call(sklearn_logistic, n_single, repeats=3)
    cost["forest_us"] = median_time_per_call(sklearn_forest, n_single, repeats=3)

    cost["logistic_numbers"] = 3 * len(model["feature_names"]) + 2
    cost["model_json_bytes"] = os.path.getsize(MODEL_PATH)
    nodes = 0
    for tree in forest.estimators_:
        nodes = nodes + tree.tree_.node_count
    cost["forest_trees"] = len(forest.estimators_)
    cost["forest_nodes"] = nodes
    cost["forest_pickle_bytes"] = len(pickle.dumps(forest))
    return cost


def fmt(value, digits=3):
    if value is None:
        return "-"
    return ("%." + str(digits) + "f") % value


METRIC_COLUMNS = [("Acc", "accuracy"), ("BalAcc", "balanced_accuracy"), ("Prec", "precision"),
                  ("Recall", "recall"), ("Spec", "specificity"), ("F1", "f1"), ("F2", "f2"),
                  ("MCC", "mcc"), ("Kappa", "kappa"), ("ROC-AUC", "roc_auc"), ("PR-AUC", "pr_auc")]


def metric_table(lines, rows, title):
    header = "%-26s" % "Method"
    for short, key in METRIC_COLUMNS:
        header = header + " %7s" % short
    lines.append(title)
    lines.append(header)
    lines.append("-" * len(header))
    saved = {}
    for name, prob_col, alarm_col in METHODS:
        m = full_metrics(rows, prob_col, alarm_col)
        line = "%-26s" % name
        for short, key in METRIC_COLUMNS:
            line = line + " %7s" % fmt(m[key])
        lines.append(line)
        saved[name.strip()] = m
    lines.append("")
    return saved


def draw_figure(rows, calib, sweep_rows, threshold, show):
    y = column(rows, "y")
    fig, axes = plt.subplots(2, 2, figsize=(11, 8.4))
    curves = [("risk", "Logistic regression", BLUE, "-"), ("p_forest", "Random forest", GREEN, "-"),
              ("p_no_graph", "LR w/o graph features", ORANGE, "--"),
              ("p_shuffled", "Shuffled labels", GREY, ":")]

    ax = axes[0][0]
    for col, label, colour, dash in curves:
        fpr, tpr, cut = roc_curve(y, column(rows, col))
        ax.plot(fpr, tpr, color=colour, linestyle=dash,
                label="%s (AUC %.3f)" % (label, roc_auc_score(y, column(rows, col))))
    ax.plot([0, 1], [0, 1], color=GREY, linewidth=0.8)
    ax.set_xlabel("False positive rate")
    ax.set_ylabel("True positive rate")
    ax.set_title("(a) ROC curves")
    ax.legend(loc="lower right", fontsize=8)

    ax = axes[0][1]
    for col, label, colour, dash in curves:
        precision, recall, cut = precision_recall_curve(y, column(rows, col))
        ax.plot(recall, precision, color=colour, linestyle=dash,
                label="%s (AP %.3f)" % (label, average_precision_score(y, column(rows, col))))
    ax.axhline(float(sum(y)) / len(y), color=GREY, linewidth=0.8)
    ax.set_xlabel("Recall")
    ax.set_ylabel("Precision")
    ax.set_ylim(0, 1.03)
    ax.set_title("(b) Precision-recall curves")
    ax.legend(loc="lower left", bbox_to_anchor=(0.02, 0.2), fontsize=8)

    ax = axes[1][0]
    for col, label, colour in [("risk", "Logistic regression", BLUE), ("p_forest", "Random forest", GREEN)]:
        table = calib[col]["table"]
        ax.plot([b["mean_risk"] for b in table], [b["actual_rate"] for b in table], color=colour,
                marker="o", label="%s (ECE %.3f)" % (label, calib[col]["ece"]))
    ax.plot([0, 1], [0, 1], color=GREY, linewidth=0.8, linestyle="--", label="Perfect calibration")
    ax.set_xlabel("Predicted risk (bin mean)")
    ax.set_ylabel("Observed livelock rate")
    ax.set_title("(c) Calibration")
    ax.legend(loc="upper left", fontsize=8)

    ax = axes[1][1]
    xs = [s["threshold"] for s in sweep_rows]
    ax.plot(xs, [s["precision"] for s in sweep_rows], color=GREEN, marker="o", label="Precision")
    ax.plot(xs, [s["recall"] for s in sweep_rows], color=ORANGE, marker="o", label="Recall")
    ax.plot(xs, [s["f1"] for s in sweep_rows], color=BLUE, marker="o", label="F1")
    ax.plot(xs, [s["healthy_alarm_pct"] / 100.0 for s in sweep_rows], color=RED, marker="o",
            label="Alarms in healthy windows")
    ax.axvline(threshold, color=GREY, linestyle="--", linewidth=1)
    ax.set_xlabel("Decision threshold")
    ax.set_ylim(0, 1.03)
    ax.set_title("(d) Threshold sweep (dashed: %.2f)" % threshold)
    ax.legend(loc="center right", bbox_to_anchor=(1.0, 0.42), fontsize=8)
    fig.tight_layout()
    return save_figure(fig, "evaluation.png", show)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--no_show", action="store_true")
    args = parser.parse_args()
    make_dirs()
    for path in [OOF_PATH, MODEL_PATH, BASELINES_PATH]:
        if not os.path.exists(path):
            print("error: %s not found, run prediction_model.py first" % path)
            sys.exit(1)
    rows = load_rows()
    with open(MODEL_PATH, "r") as f:
        model = json.load(f)

    y = column(rows, "y")
    onset_rows = [r for r in rows if r["onset"]]
    saved = {"n": len(rows), "positives": sum(y), "n_onset": len(onset_rows),
             "positives_onset": sum(column(onset_rows, "y")), "threshold": model["threshold"]}

    lines = []
    lines.append("MODEL EVALUATION  |  out-of-fold scores  |  %d windows (%d positive)  |  onset: %d windows (%d positive)"
                 % (len(rows), sum(y), len(onset_rows), saved["positives_onset"]))
    lines.append("")
    saved["all"] = metric_table(lines, rows, "Table 1. All windows")
    saved["onset"] = metric_table(lines, onset_rows, "Table 2. Onset windows")
    ours = saved["all"]["Logistic regression"]
    ours_onset = saved["onset"]["Logistic regression"]

    pred = column(rows, OURS_ALARM)
    worst = max(abs(ours["mcc"] - matthews_corrcoef(y, pred)),
                abs(ours["kappa"] - cohen_kappa_score(y, pred)),
                abs(ours["accuracy"] - accuracy_score(y, pred)))
    saved["self_check"] = worst

    lines.append("Table 3. Calibration")
    header = "%-26s %8s %8s %9s %8s" % ("Method", "Brier", "BSS", "LogLoss", "ECE")
    lines.append(header)
    lines.append("-" * len(header))
    calib = {}
    saved["calibration"] = {}
    for name, prob_col, alarm_col in METHODS:
        if prob_col is None:
            continue
        c = calibration(y, column(rows, prob_col))
        calib[prob_col] = c
        saved["calibration"][name.strip()] = c
        lines.append("%-26s %8s %8s %9s %8s" % (name, fmt(c["brier"]), fmt(c["brier_skill"]),
                                                 fmt(c["log_loss"]), fmt(c["ece"])))
    lines.append("")

    lines.append("Table 4. Reliability (logistic regression)")
    header = "%-10s %8s %10s %10s" % ("Risk bin", "Windows", "Mean p", "Observed")
    lines.append(header)
    lines.append("-" * len(header))
    for b in calib[OURS]["table"]:
        lines.append("%-10s %8d %10.3f %10.3f" % ("%.1f-%.1f" % (b["low"], b["high"]), b["n"], b["mean_risk"],
                                                b["actual_rate"]))
    lines.append("")

    lines.append("Table 5. 95%% bootstrap intervals (%d resamples of whole runs)" % BOOTSTRAP_ROUNDS)
    saved["bootstrap"] = {}
    ours_cols = (OURS, OURS_ALARM)
    checks = [
        ("Accuracy", rows, metric_on(OURS, OURS_ALARM, "accuracy"), ours["accuracy"]),
        ("Precision", rows, metric_on(OURS, OURS_ALARM, "precision"), ours["precision"]),
        ("Recall", rows, metric_on(OURS, OURS_ALARM, "recall"), ours["recall"]),
        ("F1", rows, metric_on(OURS, OURS_ALARM, "f1"), ours["f1"]),
        ("MCC", rows, metric_on(OURS, OURS_ALARM, "mcc"), ours["mcc"]),
        ("ROC-AUC", rows, metric_on(OURS, OURS_ALARM, "roc_auc"), ours["roc_auc"]),
        ("PR-AUC", rows, metric_on(OURS, OURS_ALARM, "pr_auc"), ours["pr_auc"]),
        ("F1, onset", onset_rows, metric_on(OURS, OURS_ALARM, "f1"), ours_onset["f1"]),
        ("PR-AUC, onset", onset_rows, metric_on(OURS, OURS_ALARM, "pr_auc"), ours_onset["pr_auc"]),
        ("F1 gain vs reactive rule", rows, difference(ours_cols, (None, "rule_reactive"), "f1"),
         ours["f1"] - saved["all"]["Reactive rule"]["f1"]),
        ("F1 gain vs reactive, onset", onset_rows, difference(ours_cols, (None, "rule_reactive"), "f1"),
         ours_onset["f1"] - saved["onset"]["Reactive rule"]["f1"]),
        ("PR-AUC gain, graph, onset", onset_rows, difference(ours_cols, ("p_no_graph", "a_no_graph"), "pr_auc"),
         ours_onset["pr_auc"] - saved["onset"]["w/o graph features"]["pr_auc"]),
        ("PR-AUC gap vs forest", rows, difference(ours_cols, ("p_forest", "a_forest"), "pr_auc"),
         ours["pr_auc"] - saved["all"]["Random forest"]["pr_auc"]),
    ]
    header = "%-28s %8s %8s %8s" % ("Quantity", "Value", "Low", "High")
    lines.append(header)
    lines.append("-" * len(header))
    for title, subset_rows, metric, value in checks:
        low, high, used = bootstrap(subset_rows, metric)
        lines.append("%-28s %8s %8s %8s" % (title, fmt(value), fmt(low), fmt(high)))
        saved["bootstrap"][title] = {"value": value, "low": low, "high": high, "rounds_used": used}
    lines.append("")

    lines.append("Table 6. Threshold sweep (logistic regression)")
    sweep_rows = sweep(rows)
    saved["sweep"] = sweep_rows
    header = "%-6s %7s %7s %7s %7s %7s %7s %9s %8s %8s" % ("Thr", "Acc", "Prec", "Recall", "F1", "MCC", "FPR",
                                                          "Healthy%", "Warned", "Lead(s)")
    lines.append(header)
    lines.append("-" * len(header))
    for s in sweep_rows:
        lead = "-" if s["median_lead"] is None else "%.0f" % s["median_lead"]
        mark = " *" if abs(s["threshold"] - model["threshold"]) < 1e-9 else ""
        lines.append("%-6s %7s %7s %7s %7s %7s %7s %9.1f %8s %8s%s"
                     % ("%.2f" % s["threshold"], fmt(s["accuracy"]), fmt(s["precision"]), fmt(s["recall"]),
                        fmt(s["f1"]), fmt(s["mcc"]), fmt(s["fpr"]), s["healthy_alarm_pct"],
                        "%d/%d" % (s["warned"], s["episodes"]), lead, mark))
    lines.append("")

    lines.append("Table 7. Breakdown (logistic regression)")
    info = scenario_info()
    groups = []
    for edge in EDGES:
        name = edge[0] + "->" + edge[1]
        groups.append((name.replace("_service", ""), [r for r in rows if r["edge"] == name]))
    kinds = []
    for r in rows:
        fault = info.get(r["scenario_id"], ("unknown", "unknown"))[0]
        if fault not in kinds:
            kinds.append(fault)
    for kind in sorted(kinds):
        groups.append((kind, [r for r in rows if info.get(r["scenario_id"], ("unknown",))[0] == kind]))
    for queue in ["Slot wait 1 s", "Slot wait 4-10 s"]:
        groups.append((queue, [r for r in rows if info.get(r["scenario_id"], ("", ""))[1] == queue]))
    header = "%-26s %8s %8s %7s %7s %7s %7s %8s %5s" % ("Group", "Windows", "Positive", "Prec", "Recall", "F1",
                                                      "MCC", "ROC-AUC", "FP")
    lines.append(header)
    lines.append("-" * len(header))
    saved["breakdown"] = {}
    for title, group_rows in groups:
        if len(group_rows) == 0:
            continue
        m = full_metrics(group_rows, OURS, OURS_ALARM)
        positives = sum(column(group_rows, "y"))
        if positives == 0:
            lines.append("%-26s %8d %8d %7s %7s %7s %7s %8s %5d" % (title, len(group_rows), 0, "-", "-", "-", "-",
                                                                   "-", m["fp"]))
            saved["breakdown"][title] = {"n": len(group_rows), "positives": 0, "fp": m["fp"]}
            continue
        lines.append("%-26s %8d %8d %7s %7s %7s %7s %8s %5d"
                     % (title, len(group_rows), positives, fmt(m["precision"]), fmt(m["recall"]),
                        fmt(m["f1"]), fmt(m["mcc"]), fmt(m["roc_auc"]), m["fp"]))
        saved["breakdown"][title] = {"n": len(group_rows), "positives": positives,
                                     "precision": m["precision"], "recall": m["recall"], "f1": m["f1"],
                                     "mcc": m["mcc"], "roc_auc": m["roc_auc"], "fp": m["fp"]}
    lines.append("")

    lines.append("Table 8. Run-time cost")
    cost = runtime_cost(model)
    saved["runtime"] = cost
    header = "%-44s %14s" % ("Item", "Value")
    lines.append(header)
    lines.append("-" * len(header))
    lines.append("%-44s %11.1f us" % ("Risk, one edge (own code)", cost["hand_risk_us"]))
    lines.append("%-44s %11.1f us" % ("Risk, one window (scikit-learn)", cost["sklearn_logistic_us"]))
    lines.append("%-44s %11.1f us" % ("Random forest, one window", cost["forest_us"]))
    lines.append("%-44s %11.1f us" % ("Features, one tick (3 edges)", cost["features_per_tick_us"]))
    lines.append("%-44s %11.1f us" % ("Controller round (3 edges)", cost["controller_tick_us"]))
    lines.append("%-44s %12.4f %%" % ("CPU share of the 1 s control period", cost["controller_cpu_pct"]))
    lines.append("%-44s %11.0f ms" % ("Training, logistic regression", cost["train_logistic_ms"]))
    lines.append("%-44s %11.0f ms" % ("Training, random forest", cost["train_forest_ms"]))
    lines.append("%-44s %14d" % ("Model size, logistic regression (numbers)", cost["logistic_numbers"]))
    lines.append("%-44s %8d bytes" % ("model.json", cost["model_json_bytes"]))
    lines.append("%-44s %14s" % ("Random forest (trees / nodes)", "%d / %d" % (cost["forest_trees"], cost["forest_nodes"])))
    lines.append("%-44s %11d KB" % ("Random forest, pickled", cost["forest_pickle_bytes"] // 1024))
    lines.append("")

    with open(BASELINES_PATH, "r") as f:
        baselines = json.load(f)
    examples = prediction_model.build_examples(prediction_model.load_windows(), baselines)
    y_examples = prediction_model.labels(examples)
    control_roc = []
    control_pr = []
    for seed in range(1, CONTROL_REPEATS + 1):
        probs, thresholds, fold_thresholds = prediction_model.cross_validate(examples, FEATURE_NAMES, "logistic",
                                                                             True, seed)
        control_roc.append(roc_auc_score(y_examples, probs))
        control_pr.append(average_precision_score(y_examples, probs))
    reach = 0
    for v in control_pr:
        if v >= ours["pr_auc"]:
            reach = reach + 1
    lines.append("Table 9. Shuffled-label control (%d repeats)" % CONTROL_REPEATS)
    header = "%-10s %8s %8s %8s %8s" % ("Metric", "Mean", "Min", "Max", "Ours")
    lines.append(header)
    lines.append("-" * len(header))
    lines.append("%-10s %8s %8s %8s %8s" % ("ROC-AUC", fmt(sum(control_roc) / len(control_roc)), fmt(min(control_roc)),
                                            fmt(max(control_roc)), fmt(ours["roc_auc"])))
    lines.append("%-10s %8s %8s %8s %8s" % ("PR-AUC", fmt(sum(control_pr) / len(control_pr)), fmt(min(control_pr)),
                                            fmt(max(control_pr)), fmt(ours["pr_auc"])))
    lines.append("")
    lines.append("Self-check |MCC, kappa, accuracy (own code) - scikit-learn| max = %.1e" % worst)
    saved["control_repeats"] = {"roc_auc": control_roc, "pr_auc": control_pr, "reached_ours": reach}

    with open(JSON_PATH, "w") as f:
        json.dump(saved, f, indent=2)
    text = "\n".join(lines)
    print(text)
    with open(REPORT_PATH, "w") as f:
        f.write(text + "\n")
    print("")
    draw_figure(rows, calib, sweep_rows, model["threshold"], not args.no_show)


if __name__ == "__main__":
    main()
