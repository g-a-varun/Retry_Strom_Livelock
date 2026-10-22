import argparse
import csv
import json
import os
import random
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from sklearn.ensemble import RandomForestClassifier
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import (precision_score, recall_score, f1_score,
                             average_precision_score, confusion_matrix)
from sklearn.model_selection import GroupKFold
from sklearn.preprocessing import StandardScaler

from common.config import DATA_DIR, RESULTS_DIR, EDGES, edge_name, make_dirs
from matplotlib.ticker import MaxNLocator

from common.plots import plt, save_figure, BLUE, ORANGE, GREEN, RED, GREY
from features import (make_features, feature_vector, risk_probability, reactive_alarm,
                      FEATURE_NAMES, TEMPORAL_FEATURES, GRAPH_FEATURES, WINDOW_SEC)

LABELED_PATH = os.path.join(DATA_DIR, "labeled_windows.csv")
BASELINES_PATH = os.path.join(DATA_DIR, "baselines.json")
MODEL_PATH = os.path.join(RESULTS_DIR, "model.json")
OOF_PATH = os.path.join(RESULTS_DIR, "prediction_oof.csv")
REPORT_PATH = os.path.join(RESULTS_DIR, "prediction_results.txt")
METRICS_PATH = os.path.join(RESULTS_DIR, "prediction_metrics.json")

GAP_WINDOWS = 1
HORIZON_WINDOWS = 5
N_FOLDS = 5
N_INNER_FOLDS = 4
THRESHOLD_GRID = [0.30, 0.35, 0.40, 0.45, 0.50, 0.55, 0.60, 0.65, 0.70, 0.75, 0.80, 0.85, 0.90]

STAT_FIELDS = ["n_calls", "lambda_base", "lambda_retry", "rho", "s_useful", "concurrency",
               "p_success", "fail_ratio", "overload_ratio", "mean_latency_ms"]


def to_number(text):
    if text == "" or text is None:
        return None
    return float(text)


def load_windows():
    with open(LABELED_PATH, "r") as f:
        rows = list(csv.DictReader(f))

    scenarios = {}
    for row in rows:
        scenario_id = row["scenario_id"]
        index = int(row["window_index"])
        edge = (row["source_service"], row["dest_service"])
        if scenario_id not in scenarios:
            scenarios[scenario_id] = {}
        if index not in scenarios[scenario_id]:
            scenarios[scenario_id][index] = {}

        stats = {}
        for field in STAT_FIELDS:
            stats[field] = to_number(row[field])
        stats["n_calls"] = int(stats["n_calls"])
        stats["label"] = row["label"]
        stats["phase"] = row["phase"]
        stats["t_rel"] = float(row["t_rel"])
        scenarios[scenario_id][index][edge] = stats
    return scenarios


def build_examples(scenarios, baselines):
    examples = []
    for scenario_id in sorted(scenarios):
        windows = scenarios[scenario_id]
        n_windows = max(windows.keys()) + 1

        for i in range(n_windows):
            if i not in windows:
                continue
            future_start = i + GAP_WINDOWS
            future_end = i + GAP_WINDOWS + HORIZON_WINDOWS
            if future_end > n_windows:
                continue

            stats_now = windows[i]
            stats_prev = windows.get(i - 1, {})
            values_by_edge = make_features(stats_now, stats_prev, baselines)

            for edge in EDGES:
                if edge not in stats_now:
                    continue
                now = stats_now[edge]
                if now["n_calls"] == 0 or now["phase"] == "after_end":
                    continue

                future_label = 0
                for j in range(future_start, future_end):
                    if j in windows and edge in windows[j] and windows[j][edge]["label"] == "livelock":
                        future_label = 1
                        break

                prev_label = "normal"
                if edge in stats_prev:
                    prev_label = stats_prev[edge]["label"]

                values = values_by_edge[edge]
                examples.append({
                    "scenario_id": scenario_id,
                    "edge": edge,
                    "window_index": i,
                    "t_rel": now["t_rel"],
                    "phase": now["phase"],
                    "values": values,
                    "label": future_label,
                    "label_now": 1 if now["label"] == "livelock" else 0,
                    "label_prev": 1 if prev_label == "livelock" else 0,
                    "rho_now": now["rho"],
                    "reactive_now": 1 if reactive_alarm(now) else 0,
                })
    return examples


def matrix(examples, feature_names):
    x = []
    for example in examples:
        x.append(feature_vector(example["values"], feature_names))
    return x


def labels(examples):
    y = []
    for example in examples:
        y.append(example["label"])
    return y


def make_logistic():
    return LogisticRegression(class_weight="balanced", max_iter=5000, C=1.0)


def make_forest():
    return RandomForestClassifier(n_estimators=100, max_depth=6, min_samples_leaf=3,
                                  class_weight="balanced", random_state=0)


def fit_and_score(model_kind, x_train, y_train, x_test):
    if sum(y_train) == 0 or sum(y_train) == len(y_train):
        rate = float(sum(y_train)) / max(len(y_train), 1)
        return [rate] * len(x_test)
    scaler = StandardScaler()
    x_train_scaled = scaler.fit_transform(x_train)
    x_test_scaled = scaler.transform(x_test)
    if model_kind == "forest":
        model = make_forest()
    else:
        model = make_logistic()
    model.fit(x_train_scaled, y_train)
    return [float(p) for p in model.predict_proba(x_test_scaled)[:, 1]]


def grouped_oof(model_kind, x, y, groups, n_folds):
    probs = [0.0] * len(y)
    splitter = GroupKFold(n_splits=n_folds)
    for train_index, test_index in splitter.split(x, y, groups):
        scores = fit_and_score(model_kind,
                               [x[i] for i in train_index], [y[i] for i in train_index],
                               [x[i] for i in test_index])
        k = 0
        for i in test_index:
            probs[i] = scores[k]
            k = k + 1
    return probs


def f1_at(y, probs, threshold):
    tp = 0
    fp = 0
    fn = 0
    for i in range(len(y)):
        alarm = probs[i] >= threshold
        if alarm and y[i] == 1:
            tp = tp + 1
        elif alarm:
            fp = fp + 1
        elif y[i] == 1:
            fn = fn + 1
    if tp == 0:
        return 0.0
    return 2.0 * tp / (2.0 * tp + fp + fn)


def best_threshold(y, probs):
    best = THRESHOLD_GRID[0]
    best_f1 = -1.0
    for t in THRESHOLD_GRID:
        f1 = f1_at(y, probs, t)
        if f1 > best_f1 + 1e-12:
            best = t
            best_f1 = f1
    return best


def cross_validate(examples, feature_names, model_kind, shuffle_labels, shuffle_seed=0):
    groups = []
    for example in examples:
        groups.append(example["scenario_id"])

    x_all = matrix(examples, feature_names)
    y_all = labels(examples)
    probs = [0.0] * len(examples)
    thresholds = [0.5] * len(examples)
    fold_thresholds = []

    splitter = GroupKFold(n_splits=N_FOLDS)
    fold_number = 0
    for train_index, test_index in splitter.split(x_all, y_all, groups):
        x_train = [x_all[i] for i in train_index]
        y_train = [y_all[i] for i in train_index]
        g_train = [groups[i] for i in train_index]
        x_test = [x_all[i] for i in test_index]

        if shuffle_labels:
            rng = random.Random(fold_number + 1000 * shuffle_seed)
            rng.shuffle(y_train)

        inner = grouped_oof(model_kind, x_train, y_train, g_train, N_INNER_FOLDS)
        threshold = best_threshold(y_train, inner)
        fold_thresholds.append(threshold)

        scores = fit_and_score(model_kind, x_train, y_train, x_test)
        k = 0
        for i in test_index:
            probs[i] = scores[k]
            thresholds[i] = threshold
            k = k + 1
        fold_number = fold_number + 1

    return probs, thresholds, fold_thresholds


def score(y_true, y_pred, probs):
    tn, fp, fn, tp = confusion_matrix(y_true, y_pred, labels=[0, 1]).ravel().tolist()
    n = len(y_true)
    denominator = ((tp + fp) * (tp + fn) * (tn + fp) * (tn + fn)) ** 0.5
    result = {
        "accuracy": float(tp + tn) / n if n else 0.0,
        "precision": precision_score(y_true, y_pred, zero_division=0),
        "recall": recall_score(y_true, y_pred, zero_division=0),
        "f1": f1_score(y_true, y_pred, zero_division=0),
        "mcc": (tp * tn - fp * fn) / denominator if denominator > 0 else 0.0,
        "positives": sum(y_true),
        "n": n,
    }
    if probs is not None and 0 < sum(y_true) < len(y_true):
        result["pr_auc"] = average_precision_score(y_true, probs)
    else:
        result["pr_auc"] = None
    return result


def alarms_from(probs, thresholds):
    pred = []
    for i in range(len(probs)):
        pred.append(1 if probs[i] >= thresholds[i] else 0)
    return pred


def subset(values, keep):
    result = []
    for i in range(len(values)):
        if keep[i]:
            result.append(values[i])
    return result


def episodes(examples):
    labelled = {}
    for example in examples:
        key = (example["scenario_id"], example["edge"])
        if key not in labelled:
            labelled[key] = {}
        labelled[key][example["window_index"]] = example["label_now"]

    found = []
    for key in labelled:
        by_index = labelled[key]
        for i in sorted(by_index):
            if by_index[i] == 1 and by_index.get(i - 1, 0) == 0:
                found.append((key[0], key[1], i))
    return found


def lead_times(examples, alarms):
    alarm_at = {}
    for i in range(len(examples)):
        example = examples[i]
        alarm_at[(example["scenario_id"], example["edge"], example["window_index"])] = alarms[i]

    leads = []
    missed = 0
    for scenario_id, edge, onset in episodes(examples):
        earliest = None
        j = onset - HORIZON_WINDOWS
        while j < onset:
            if alarm_at.get((scenario_id, edge, j), 0) == 1:
                earliest = j
                break
            j = j + 1
        if earliest is None:
            missed = missed + 1
        else:
            leads.append((onset - earliest) * WINDOW_SEC)
    return leads, missed


def median_of(values):
    if len(values) == 0:
        return None
    ordered = sorted(values)
    middle = len(ordered) // 2
    if len(ordered) % 2 == 1:
        return ordered[middle]
    return (ordered[middle - 1] + ordered[middle]) / 2.0


def false_alarm_rate(examples, alarms):
    healthy = 0
    raised = 0
    for i in range(len(examples)):
        if examples[i]["phase"] == "normal":
            healthy = healthy + 1
            if alarms[i] == 1:
                raised = raised + 1
    return raised, healthy


def fmt(value, digits=3):
    if value is None:
        return "-"
    return "%.*f" % (digits, value)


METHODS = [
    ("Logistic regression", "logistic", "all", False),
    ("  w/o graph features", "logistic", "no_graph", False),
    ("  w/o temporal features", "logistic", "no_temporal", False),
    ("Random forest", "forest", "all", False),
    ("Shuffled labels", "logistic", "all", True),
]
RULES = ["Reactive rule", "Persistence rule"]
OURS = "Logistic regression"


def draw(saved, model, show):
    fig = plt.figure(figsize=(11, 8))
    grid = fig.add_gridspec(2, 2, height_ratios=[1, 1.15])
    ax_a = fig.add_subplot(grid[0, :])
    ax_b = fig.add_subplot(grid[1, 0])
    ax_c = fig.add_subplot(grid[1, 1])

    names = ["Logistic regression", "w/o graph features", "w/o temporal features", "Random forest",
             "Reactive rule", "Persistence rule", "Shuffled labels"]
    labels = ["Logistic\nregression", "w/o graph", "w/o temporal", "Random\nforest", "Reactive\nrule",
              "Persistence\nrule", "Shuffled\nlabels"]
    metrics = [("precision", "Precision", BLUE), ("recall", "Recall", ORANGE), ("f1", "F1", GREEN),
               ("mcc", "MCC", RED)]
    width = 0.2
    for k in range(len(metrics)):
        key, label, colour = metrics[k]
        xs = [i + (k - 1.5) * width for i in range(len(names))]
        values = [saved["all"][n][key] for n in names]
        ax_a.bar(xs, values, width=width, color=colour, label=label)
        ax_a.grid(False, axis="x")
    ax_a.set_xticks(range(len(names)))
    ax_a.set_xticklabels(labels, fontsize=9)
    ax_a.set_ylim(-0.25, 1.05)
    ax_a.axhline(0, color="black", linewidth=0.8)
    ax_a.grid(axis="x", visible=False)
    ax_a.set_title("(a) Out-of-fold scores, runs never seen in training")
    ax_a.legend(ncol=4, loc="upper right")

    bins = [2, 4, 6, 8, 10]
    for offset, key, colour, label in [(-0.4, "our model", BLUE, "Logistic regression"),
                                       (0.4, "reactive rule", GREY, "Reactive rule")]:
        data = saved["early_warning"][key]
        counts = [len([x for x in data["lead_times"] if abs(x - b) < 0.01]) for b in bins]
        ax_b.bar([b + offset for b in bins], counts, width=0.8, color=colour,
                 label="%s: %d/%d warned" % (label, len(data["lead_times"]), data["episodes"]))
        ax_b.grid(False, axis="x")
    ax_b.set_xticks(bins)
    ax_b.set_xlabel("Warning before livelock onset (s)")
    ax_b.set_ylabel("Episodes")
    ax_b.grid(axis="x", visible=False)
    ax_b.yaxis.set_major_locator(MaxNLocator(integer=True))
    ax_b.set_title("(b) Early warning")
    ax_b.legend(loc="upper right")

    pairs = []
    for i in range(len(model["feature_names"])):
        pairs.append((abs(model["weights"][i]), model["feature_names"][i], model["weights"][i]))
    pairs.sort()
    ax_c.barh(range(len(pairs)), [p[2] for p in pairs], color=[BLUE if p[2] > 0 else ORANGE for p in pairs])
    ax_c.set_yticks(range(len(pairs)))
    ax_c.set_yticklabels([p[1] for p in pairs], fontsize=8)
    ax_c.axvline(0, color="black", linewidth=0.8)
    ax_c.grid(axis="y", visible=False)
    ax_c.set_xlabel("Weight (standardised feature)")
    ax_c.set_title("(c) Final model weights, threshold %.2f" % model["threshold"])
    fig.tight_layout()
    return save_figure(fig, "prediction.png", show)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--no_show", action="store_true")
    args = parser.parse_args()
    make_dirs()
    if not os.path.exists(LABELED_PATH) or not os.path.exists(BASELINES_PATH):
        print("error: run labeling.py first")
        sys.exit(1)

    with open(BASELINES_PATH, "r") as f:
        baselines = json.load(f)
    for edge in EDGES:
        if edge_name(edge) not in baselines:
            print("error: no baseline for %s" % edge_name(edge))
            sys.exit(1)

    scenarios = load_windows()
    examples = build_examples(scenarios, baselines)
    y = labels(examples)
    n_scenarios = len(scenarios)
    if sum(y) < 5 or n_scenarios < N_FOLDS:
        print("error: not enough data (%d runs, %d positive windows)" % (n_scenarios, sum(y)))
        sys.exit(1)

    feature_sets = {"all": FEATURE_NAMES, "no_graph": [], "no_temporal": []}
    for name in FEATURE_NAMES:
        if name not in GRAPH_FEATURES:
            feature_sets["no_graph"].append(name)
        if name not in TEMPORAL_FEATURES:
            feature_sets["no_temporal"].append(name)

    print("Cross-validating %d models (%d folds grouped by run, nested threshold search)..."
          % (len(METHODS), N_FOLDS), flush=True)
    probs_by = {}
    alarms_by = {}
    fold_thr_by = {}
    for title, kind, feature_set, shuffle in METHODS:
        probs, thresholds, fold_thresholds = cross_validate(examples, feature_sets[feature_set], kind, shuffle)
        probs_by[title] = probs
        alarms_by[title] = alarms_from(probs, thresholds)
        fold_thr_by[title] = fold_thresholds
        if title == OURS:
            ours_thresholds = thresholds

    alarms_by["Reactive rule"] = [example["reactive_now"] for example in examples]
    alarms_by["Persistence rule"] = [example["label_now"] for example in examples]
    order = [m[0] for m in METHODS] + RULES

    saved = {"all": {}, "onset": {}, "early_warning": {}, "n_examples": len(examples),
             "n_positive": sum(y), "n_scenarios": n_scenarios, "fold_thresholds": fold_thr_by[OURS]}

    lines = []
    lines.append("LIVELOCK PREDICTION  |  horizon %d-%d s  |  %d-fold grouped CV  |  %d runs, %d windows, %d positive (%.1f%%)"
                 % (GAP_WINDOWS * WINDOW_SEC, (GAP_WINDOWS + HORIZON_WINDOWS) * WINDOW_SEC, N_FOLDS,
                    n_scenarios, len(examples), sum(y), 100.0 * sum(y) / len(y)))
    lines.append("")
    header = "%-26s %8s %8s %8s %8s %8s %8s" % ("Method", "Acc", "Prec", "Recall", "F1", "MCC", "PR-AUC")
    lines.append("Table 1. All windows")
    lines.append(header)
    lines.append("-" * len(header))
    for title in order:
        result = score(y, alarms_by[title], probs_by.get(title))
        saved["all"][title.strip()] = result
        lines.append("%-26s %8s %8s %8s %8s %8s %8s" % (title, fmt(result["accuracy"]), fmt(result["precision"]),
                                                       fmt(result["recall"]), fmt(result["f1"]),
                                                       fmt(result["mcc"]), fmt(result["pr_auc"])))
    lines.append("")

    keep = []
    for example in examples:
        keep.append(example["label_now"] == 0 and example["label_prev"] == 0)
    y_onset = subset(y, keep)
    lines.append("Table 2. Onset windows (n = %d, positive = %d)" % (len(y_onset), sum(y_onset)))
    lines.append(header)
    lines.append("-" * len(header))
    for title in order:
        probs = None
        if title in probs_by:
            probs = subset(probs_by[title], keep)
        result = score(y_onset, subset(alarms_by[title], keep), probs)
        saved["onset"][title.strip()] = result
        lines.append("%-26s %8s %8s %8s %8s %8s %8s" % (title, fmt(result["accuracy"]), fmt(result["precision"]),
                                                       fmt(result["recall"]), fmt(result["f1"]),
                                                       fmt(result["mcc"]), fmt(result["pr_auc"])))
    lines.append("")

    n_episodes = len(episodes(examples))
    lines.append("Table 3. Early warning (%d livelock episodes)" % n_episodes)
    header3 = "%-26s %8s %12s %10s %16s" % ("Method", "Warned", "Median (s)", "Range (s)", "Healthy alarms")
    lines.append(header3)
    lines.append("-" * len(header3))
    for title, key in [(OURS, "our model"), ("Reactive rule", "reactive rule")]:
        leads, missed = lead_times(examples, alarms_by[title])
        raised, healthy = false_alarm_rate(examples, alarms_by[title])
        span = "-"
        middle = "-"
        if len(leads) > 0:
            span = "%.0f-%.0f" % (min(leads), max(leads))
            middle = "%.1f" % median_of(leads)
        lines.append("%-26s %8s %12s %10s %16s" % (title, "%d/%d" % (len(leads), n_episodes), middle, span,
                                                    "%d/%d" % (raised, healthy)))
        saved["early_warning"][key] = {"lead_times": leads, "missed": missed, "episodes": n_episodes,
                                       "healthy_alarms": raised, "healthy_windows": healthy}
    lines.append("")

    cm = confusion_matrix(y, alarms_by[OURS], labels=[0, 1]).tolist()
    saved["confusion_matrix"] = cm

    groups = [example["scenario_id"] for example in examples]
    x_all = matrix(examples, FEATURE_NAMES)
    final_threshold = best_threshold(y, grouped_oof("logistic", x_all, y, groups, N_FOLDS))
    scaler = StandardScaler()
    x_scaled = scaler.fit_transform(x_all)
    final = make_logistic()
    final.fit(x_scaled, y)

    model = {
        "feature_names": FEATURE_NAMES,
        "mean": [float(v) for v in scaler.mean_],
        "std": [float(v) for v in scaler.scale_],
        "weights": [float(v) for v in final.coef_[0]],
        "intercept": float(final.intercept_[0]),
        "threshold": final_threshold,
        "baselines": baselines,
        "horizon_sec": [GAP_WINDOWS * WINDOW_SEC, (GAP_WINDOWS + HORIZON_WINDOWS) * WINDOW_SEC],
        "trained_on_examples": len(examples),
        "trained_on_scenarios": n_scenarios,
    }

    sk_probs = final.predict_proba(x_scaled)[:, 1]
    worst = 0.0
    for i in range(len(examples)):
        diff = abs(risk_probability(model, examples[i]["values"]) - sk_probs[i])
        if diff > worst:
            worst = diff

    lines.append("Confusion matrix (logistic regression):  TN = %d   FP = %d   FN = %d   TP = %d"
                 % (cm[0][0], cm[0][1], cm[1][0], cm[1][1]))
    lines.append("Decision threshold (nested CV, per fold): %s"
                 % ", ".join("%.2f" % t for t in fold_thr_by[OURS]))
    lines.append("Final model: %d features, threshold %.2f, trained on %d windows -> results/model.json"
                 % (len(FEATURE_NAMES), final_threshold, len(examples)))
    lines.append("Self-check |p(own code) - p(scikit-learn)| max = %.1e" % worst)

    weight_lines = ["", "Table 4. Standardised weights (final model)", "%-28s %8s" % ("Feature", "Weight"), "-" * 37]
    pairs = []
    for i in range(len(FEATURE_NAMES)):
        pairs.append((abs(model["weights"][i]), FEATURE_NAMES[i], model["weights"][i]))
    pairs.sort(reverse=True)
    for size, name, weight in pairs:
        weight_lines.append("%-28s %+8.3f" % (name, weight))
    weight_lines.append("%-28s %+8.3f" % ("(intercept)", model["intercept"]))

    with open(MODEL_PATH, "w") as f:
        json.dump(model, f, indent=2)

    saved["weights"] = {}
    for i in range(len(FEATURE_NAMES)):
        saved["weights"][FEATURE_NAMES[i]] = model["weights"][i]
    saved["threshold"] = final_threshold
    with open(METRICS_PATH, "w") as f:
        json.dump(saved, f, indent=2)

    with open(OOF_PATH, "w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["scenario_id", "edge", "window_index", "t_rel", "phase", "label_now",
                         "label_future", "risk", "threshold", "alarm", "rho_now", "label_prev",
                         "p_no_graph", "a_no_graph", "p_no_temporal", "a_no_temporal",
                         "p_forest", "a_forest", "p_shuffled", "a_shuffled",
                         "rule_reactive", "rule_persistence"])
        for i in range(len(examples)):
            example = examples[i]
            writer.writerow([example["scenario_id"], edge_name(example["edge"]), example["window_index"],
                             example["t_rel"], example["phase"], example["label_now"], example["label"],
                             round(probs_by[OURS][i], 6), ours_thresholds[i], alarms_by[OURS][i],
                             example["rho_now"], example["label_prev"],
                             round(probs_by["  w/o graph features"][i], 6), alarms_by["  w/o graph features"][i],
                             round(probs_by["  w/o temporal features"][i], 6), alarms_by["  w/o temporal features"][i],
                             round(probs_by["Random forest"][i], 6), alarms_by["Random forest"][i],
                             round(probs_by["Shuffled labels"][i], 6), alarms_by["Shuffled labels"][i],
                             alarms_by["Reactive rule"][i], alarms_by["Persistence rule"][i]])

    text = "\n".join(lines)
    print("")
    print(text)
    with open(REPORT_PATH, "w") as f:
        f.write(text + "\n" + "\n".join(weight_lines) + "\n")
    draw(saved, model, not args.no_show)


if __name__ == "__main__":
    main()
