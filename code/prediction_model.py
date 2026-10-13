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
from features import (make_features, feature_vector, risk_probability, reactive_alarm,
                      FEATURE_NAMES, TEMPORAL_FEATURES, GRAPH_FEATURES, WINDOW_SEC)

# Module 6: livelock prediction.
#
# Question asked for every edge, every 2 seconds:
#   "will this edge be in livelock at some point in the next 2-12 seconds?"
#
# Inputs: the features from features.py (this edge now, how it changed,
# and its neighbours in the service graph). Label: the livelock label from
# labeling.py, looked up in the FUTURE windows (never the current one).
#
# Honest evaluation:
#   - grouped cross-validation: the test scenarios are never seen in
#     training, not even other windows of the same run
#   - compared against simple baselines that need no learning
#   - a shuffled-label control (if that scores as well, the model learned nothing)
#   - ablations: without the graph features, without the temporal features
#   - lead time: how many seconds BEFORE a livelock starts the alarm goes up

LABELED_PATH = os.path.join(DATA_DIR, "labeled_windows.csv")
BASELINES_PATH = os.path.join(DATA_DIR, "baselines.json")
MODEL_PATH = os.path.join(RESULTS_DIR, "model.json")
OOF_PATH = os.path.join(RESULTS_DIR, "prediction_oof.csv")
REPORT_PATH = os.path.join(RESULTS_DIR, "prediction_results.txt")
METRICS_PATH = os.path.join(RESULTS_DIR, "prediction_metrics.json")

GAP_WINDOWS = 1
HORIZON_WINDOWS = 5
N_FOLDS = 5
THRESHOLD = 0.5

STAT_FIELDS = ["n_calls", "lambda_base", "lambda_retry", "rho", "s_useful", "concurrency",
               "p_success", "fail_ratio", "overload_ratio", "mean_latency_ms"]


def to_number(text):
    if text == "" or text is None:
        return None
    return float(text)


def load_windows():
    # returns {scenario_id: {window_index: {edge: row}}}
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
    return LogisticRegression(class_weight="balanced", max_iter=2000, C=1.0)


def make_forest():
    return RandomForestClassifier(n_estimators=200, max_depth=6, min_samples_leaf=3,
                                  class_weight="balanced", random_state=0)


def cross_validate(examples, feature_names, model_kind, shuffle_labels):
    # out-of-fold probabilities: each example is scored by a model that
    # never saw its scenario
    groups = []
    for example in examples:
        groups.append(example["scenario_id"])

    x_all = matrix(examples, feature_names)
    y_all = labels(examples)
    probs = [0.0] * len(examples)
    fold_f1 = []

    splitter = GroupKFold(n_splits=N_FOLDS)
    fold_number = 0
    for train_index, test_index in splitter.split(x_all, y_all, groups):
        x_train = [x_all[i] for i in train_index]
        y_train = [y_all[i] for i in train_index]
        x_test = [x_all[i] for i in test_index]
        y_test = [y_all[i] for i in test_index]

        if shuffle_labels:
            rng = random.Random(fold_number)
            rng.shuffle(y_train)

        if sum(y_train) == 0 or sum(y_train) == len(y_train):
            # a fold with only one class cannot be trained; predict the base rate
            for i in test_index:
                probs[i] = float(sum(y_train)) / max(len(y_train), 1)
            fold_number = fold_number + 1
            continue

        scaler = StandardScaler()
        x_train_scaled = scaler.fit_transform(x_train)
        x_test_scaled = scaler.transform(x_test)

        if model_kind == "forest":
            model = make_forest()
        else:
            model = make_logistic()
        model.fit(x_train_scaled, y_train)
        fold_probs = model.predict_proba(x_test_scaled)[:, 1]

        fold_pred = []
        k = 0
        for i in test_index:
            probs[i] = float(fold_probs[k])
            fold_pred.append(1 if fold_probs[k] >= THRESHOLD else 0)
            k = k + 1
        if sum(y_test) > 0:
            fold_f1.append(f1_score(y_test, fold_pred, zero_division=0))
        fold_number = fold_number + 1

    return probs, fold_f1


def score(y_true, y_pred, probs):
    result = {
        "precision": precision_score(y_true, y_pred, zero_division=0),
        "recall": recall_score(y_true, y_pred, zero_division=0),
        "f1": f1_score(y_true, y_pred, zero_division=0),
        "positives": sum(y_true),
        "n": len(y_true),
    }
    if probs is not None and 0 < sum(y_true) < len(y_true):
        result["pr_auc"] = average_precision_score(y_true, probs)
    else:
        result["pr_auc"] = None
    return result


def predictions_from(probs):
    pred = []
    for p in probs:
        pred.append(1 if p >= THRESHOLD else 0)
    return pred


def subset(values, keep):
    result = []
    for i in range(len(values)):
        if keep[i]:
            result.append(values[i])
    return result


def episodes(examples):
    # a livelock episode = consecutive livelock windows on one edge of one
    # scenario. Returns (scenario, edge, onset window index) for each.
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
    # for every episode: the earliest alarm in the HORIZON windows before
    # it started. lead time = how much warning that gave, in seconds.
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
    # alarms raised during healthy, fault-free traffic
    healthy = 0
    raised = 0
    for i in range(len(examples)):
        if examples[i]["phase"] == "normal":
            healthy = healthy + 1
            if alarms[i] == 1:
                raised = raised + 1
    return raised, healthy


def fmt(value):
    if value is None:
        return "  -  "
    return "%.2f" % value


def main():
    make_dirs()
    if not os.path.exists(LABELED_PATH):
        print("error: %s not found. Run labeling.py first." % LABELED_PATH)
        sys.exit(1)
    if not os.path.exists(BASELINES_PATH):
        print("error: %s not found. Run labeling.py first." % BASELINES_PATH)
        sys.exit(1)

    with open(BASELINES_PATH, "r") as f:
        baselines = json.load(f)
    for edge in EDGES:
        if edge_name(edge) not in baselines:
            print("error: no healthy baseline for %s -- the dataset has no traffic on it" % edge_name(edge))
            sys.exit(1)

    scenarios = load_windows()
    examples = build_examples(scenarios, baselines)
    y = labels(examples)
    n_scenarios = len(scenarios)

    lines = []
    lines.append("Livelock prediction -- will this edge be in livelock in the next %d-%d seconds?"
                 % (GAP_WINDOWS * WINDOW_SEC, (GAP_WINDOWS + HORIZON_WINDOWS) * WINDOW_SEC))
    lines.append("=" * 78)
    lines.append("examples: %d (edge x 2s window) from %d scenarios, %d positive (%.1f%%)"
                 % (len(examples), n_scenarios, sum(y), 100.0 * sum(y) / max(len(y), 1)))

    if sum(y) < 5 or n_scenarios < N_FOLDS:
        lines.append("")
        lines.append("NOT ENOUGH DATA: need at least %d scenarios and 5 positive examples." % N_FOLDS)
        lines.append("Run generate_dataset.py (or more scenario_runner.py runs) and labeling.py again.")
        print("\n".join(lines))
        sys.exit(1)

    lines.append("evaluation: %d-fold cross-validation grouped by scenario (test runs never seen in training)"
                 % N_FOLDS)
    lines.append("")

    no_graph = []
    no_temporal = []
    for name in FEATURE_NAMES:
        if name not in GRAPH_FEATURES:
            no_graph.append(name)
        if name not in TEMPORAL_FEATURES:
            no_temporal.append(name)

    print("cross-validating the model, the baselines and the ablations...")
    runs = [
        ("logistic regression (ours)", FEATURE_NAMES, "logistic", False),
        ("  - without graph features", no_graph, "logistic", False),
        ("  - without temporal features", no_temporal, "logistic", False),
        ("random forest (comparison)", FEATURE_NAMES, "forest", False),
        ("shuffled-label control", FEATURE_NAMES, "logistic", True),
    ]

    results = {}
    oof = {}
    for title, names, kind, shuffle in runs:
        probs, fold_f1 = cross_validate(examples, names, kind, shuffle)
        oof[title] = probs
        results[title] = (score(y, predictions_from(probs), probs), fold_f1)

    rule_rho = []
    rule_persist = []
    for example in examples:
        rule_rho.append(example["reactive_now"])
        rule_persist.append(example["label_now"])
    results["rule: storm right now (reactive)"] = (score(y, rule_rho, None), [])
    results["rule: livelock right now (persistence)"] = (score(y, rule_persist, None), [])

    order = [r[0] for r in runs] + ["rule: storm right now (reactive)",
                                    "rule: livelock right now (persistence)"]

    saved = {"all": {}, "onset": {}, "early_warning": {}, "n_examples": len(examples),
             "n_positive": sum(y), "n_scenarios": n_scenarios}
    for title in order:
        saved["all"][title.strip(" -")] = results[title][0]

    lines.append("ALL WINDOWS")
    lines.append("  %-40s %9s %7s %6s %7s %14s" % ("method", "precision", "recall", "f1", "PR-AUC", "f1 per fold"))
    for title in order:
        result, fold_f1 = results[title]
        per_fold = "-"
        if len(fold_f1) > 0:
            mean = sum(fold_f1) / len(fold_f1)
            var = 0.0
            for v in fold_f1:
                var = var + (v - mean) ** 2
            std = (var / len(fold_f1)) ** 0.5
            per_fold = "%.2f +/- %.2f" % (mean, std)
        lines.append("  %-40s %9s %7s %6s %7s %14s" % (title, fmt(result["precision"]), fmt(result["recall"]),
                                                       fmt(result["f1"]), fmt(result["pr_auc"]), per_fold))
    lines.append("  chance level (PR-AUC of a random guess) = %.2f" % (float(sum(y)) / len(y)))
    lines.append("")

    # the harder, more honest question: livelock that has NOT started yet
    keep = []
    for example in examples:
        keep.append(example["label_now"] == 0 and example["label_prev"] == 0)
    y_onset = subset(y, keep)
    lines.append("ONSET ONLY -- edge is NOT in livelock now; will it be soon? (%d examples, %d positive)"
                 % (len(y_onset), sum(y_onset)))
    lines.append("  %-40s %9s %7s %6s %7s" % ("method", "precision", "recall", "f1", "PR-AUC"))
    for title in order:
        if title in oof:
            probs = subset(oof[title], keep)
            result = score(y_onset, predictions_from(probs), probs)
        elif "reactive" in title:
            result = score(y_onset, subset(rule_rho, keep), None)
        else:
            result = score(y_onset, subset(rule_persist, keep), None)
        lines.append("  %-40s %9s %7s %6s %7s" % (title, fmt(result["precision"]), fmt(result["recall"]),
                                                  fmt(result["f1"]), fmt(result["pr_auc"])))
        saved["onset"][title.strip(" -")] = result
    lines.append("")

    ours = oof["logistic regression (ours)"]
    ours_alarm = predictions_from(ours)
    n_episodes = len(episodes(examples))
    lines.append("EARLY WARNING -- %d livelock episodes in the data" % n_episodes)
    for title, alarms in [("our model", ours_alarm), ("reactive rule", rule_rho)]:
        leads, missed = lead_times(examples, alarms)
        raised, healthy = false_alarm_rate(examples, alarms)
        caught = len(leads)
        line = "  %-24s warned before onset in %d/%d episodes" % (title, caught, n_episodes)
        if caught > 0:
            line = line + ", median warning %.1fs ahead (range %.0f-%.0fs)" % (
                median_of(leads), min(leads), max(leads))
        lines.append(line)
        lines.append("  %-24s alarms during healthy traffic: %d of %d windows (%.1f%%)"
                     % ("", raised, healthy, 100.0 * raised / max(healthy, 1)))
        saved["early_warning"][title] = {"lead_times": leads, "missed": missed, "episodes": n_episodes,
                                         "healthy_alarms": raised, "healthy_windows": healthy}
    lines.append("")

    cm = confusion_matrix(y, ours_alarm, labels=[0, 1]).tolist()
    lines.append("confusion matrix of our model, all windows [[TN, FP], [FN, TP]]: %s" % cm)
    lines.append("")

    # ---- final model on ALL data, saved for the live controller ----
    x_all = matrix(examples, FEATURE_NAMES)
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
        "threshold": THRESHOLD,
        "baselines": baselines,
        "horizon_sec": [GAP_WINDOWS * WINDOW_SEC, (GAP_WINDOWS + HORIZON_WINDOWS) * WINDOW_SEC],
        "trained_on_examples": len(examples),
        "trained_on_scenarios": n_scenarios,
    }

    # self-check: the hand-written sigmoid in features.py must give exactly
    # what scikit-learn gives, or the controller would act on wrong numbers
    sk_probs = final.predict_proba(x_scaled)[:, 1]
    worst = 0.0
    for i in range(len(examples)):
        diff = abs(risk_probability(model, examples[i]["values"]) - sk_probs[i])
        if diff > worst:
            worst = diff
    lines.append("final model trained on all %d examples, saved to results/model.json" % len(examples))
    lines.append("self-check: hand-computed risk vs scikit-learn, largest difference = %.2e (%s)"
                 % (worst, "OK" if worst < 1e-6 else "MISMATCH"))
    lines.append("")

    lines.append("what the model looks at (weight on the standardised feature;")
    lines.append("positive = pushes towards 'livelock coming'):")
    pairs = []
    for i in range(len(FEATURE_NAMES)):
        pairs.append((abs(model["weights"][i]), FEATURE_NAMES[i], model["weights"][i]))
    pairs.sort(reverse=True)
    for size, name, weight in pairs:
        lines.append("  %-24s %+.3f" % (name, weight))

    with open(MODEL_PATH, "w") as f:
        json.dump(model, f, indent=2)

    saved["weights"] = {}
    for i in range(len(FEATURE_NAMES)):
        saved["weights"][FEATURE_NAMES[i]] = model["weights"][i]
    with open(METRICS_PATH, "w") as f:
        json.dump(saved, f, indent=2)

    with open(OOF_PATH, "w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["scenario_id", "edge", "window_index", "t_rel", "phase", "label_now",
                         "label_future", "risk", "rho_now"])
        for i in range(len(examples)):
            example = examples[i]
            writer.writerow([example["scenario_id"], edge_name(example["edge"]), example["window_index"],
                             example["t_rel"], example["phase"], example["label_now"], example["label"],
                             round(ours[i], 4), example["rho_now"]])

    text = "\n".join(lines)
    print("")
    print(text)
    with open(REPORT_PATH, "w") as f:
        f.write(text + "\n")


if __name__ == "__main__":
    main()
