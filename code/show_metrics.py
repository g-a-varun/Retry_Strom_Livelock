import csv
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from common.config import RESULTS_DIR

# Shows how good the livelock prediction model is, step by step:
# the confusion matrix, then every metric with its formula and the actual
# numbers put into it. Everything is computed here with plain Python
# (loops, +, -, *, /, sqrt) -- no numpy, no scikit-learn -- so every number
# can be followed by hand.
#
# Input: results/prediction_oof.csv, written by prediction_model.py. Every
# row is one edge in one 2-second window, with the risk given by a model
# that NEVER saw that run (5-fold cross-validation, split by run).
#
#   python show_metrics.py          (takes about a second, no services needed)

OOF_PATH = os.path.join(RESULTS_DIR, "prediction_oof.csv")
MODEL_PATH = os.path.join(RESULTS_DIR, "model.json")


def load_rows():
    rows = []
    with open(OOF_PATH, "r") as f:
        for row in csv.DictReader(f):
            rows.append({
                "scenario_id": row["scenario_id"],
                "y": int(row["label_future"]),
                "risk": float(row["risk"]),
                "rule": int(row["rule_reactive"]),
            })
    return rows


def count_cells(y, pred):
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


def divide(a, b):
    if b == 0:
        return 0.0
    return float(a) / b


def square_root(x):
    # Newton's method, so even the square root is done by hand
    if x <= 0:
        return 0.0
    guess = x
    for step in range(100):
        better = (guess + x / guess) / 2.0
        if abs(better - guess) < 1e-12 * guess:
            return better
        guess = better
    return guess


def metrics_from_cells(tp, fp, tn, fn):
    total = tp + fp + tn + fn
    m = {}
    m["accuracy"] = divide(tp + tn, total)
    m["precision"] = divide(tp, tp + fp)
    m["recall"] = divide(tp, tp + fn)
    m["specificity"] = divide(tn, tn + fp)
    m["balanced_accuracy"] = (m["recall"] + m["specificity"]) / 2.0
    m["f1"] = divide(2 * m["precision"] * m["recall"], m["precision"] + m["recall"])
    bottom = square_root(float(tp + fp) * (tp + fn) * (tn + fp) * (tn + fn))
    m["mcc"] = divide(tp * tn - fp * fn, bottom)
    return m


def roc_auc_by_hand(y, scores):
    # ROC-AUC = the chance that a random window where livelock DID come gets
    # a higher risk than a random window where it did not (ties count half).
    # Checked over every (yes, no) pair.
    yes_scores = []
    no_scores = []
    for i in range(len(y)):
        if y[i] == 1:
            yes_scores.append(scores[i])
        else:
            no_scores.append(scores[i])
    wins = 0.0
    for s_yes in yes_scores:
        for s_no in no_scores:
            if s_yes > s_no:
                wins = wins + 1.0
            elif s_yes == s_no:
                wins = wins + 0.5
    return divide(wins, len(yes_scores) * len(no_scores)), len(yes_scores) * len(no_scores)


def risk_of(pair):
    return pair[0]


def pr_auc_by_hand(y, scores):
    # PR-AUC (average precision): go down the windows from the highest risk
    # to the lowest; every time recall goes up, add
    # (increase in recall) x (precision at that point).
    pairs = []
    for i in range(len(y)):
        pairs.append((scores[i], y[i]))
    pairs.sort(key=risk_of, reverse=True)
    total_yes = sum(y)
    found = 0
    seen = 0
    area = 0.0
    last_recall = 0.0
    i = 0
    while i < len(pairs):
        # windows with exactly the same risk are taken together
        j = i
        while j < len(pairs) and pairs[j][0] == pairs[i][0]:
            seen = seen + 1
            found = found + pairs[j][1]
            j = j + 1
        recall = divide(found, total_yes)
        precision = divide(found, seen)
        area = area + (recall - last_recall) * precision
        last_recall = recall
        i = j
    return area


def pct(x):
    return "%5.1f %%" % (100.0 * x)


def main():
    if not os.path.exists(OOF_PATH):
        print("error: %s not found. Run prediction_model.py first." % OOF_PATH)
        sys.exit(1)

    threshold = 0.5
    if os.path.exists(MODEL_PATH):
        with open(MODEL_PATH, "r") as f:
            threshold = json.load(f).get("threshold", 0.5)

    rows = load_rows()
    y = []
    risk = []
    rule = []
    runs = set()
    for r in rows:
        y.append(r["y"])
        risk.append(r["risk"])
        rule.append(r["rule"])
        runs.add(r["scenario_id"])

    pred = []
    for p in risk:
        if p >= threshold:
            pred.append(1)
        else:
            pred.append(0)

    tp, fp, tn, fn = count_cells(y, pred)
    total = tp + fp + tn + fn
    m = metrics_from_cells(tp, fp, tn, fn)

    print("HOW GOOD IS THE LIVELOCK PREDICTION MODEL?")
    print("=" * 78)
    print("Every number below is computed in this file with plain Python (no numpy,")
    print("no scikit-learn), so each one can be checked by hand.")
    print("")
    print("Data     : %d windows (one call edge x 2 seconds) from %d recorded runs" % (total, len(runs)))
    print("Question : will this edge be in livelock in the next 2-12 seconds?")
    print("Truth    : the 4-condition livelock test (labeling.py) on the windows that followed")
    print("Model    : risk = 1 / (1 + e^-(w.x + b)), 16 features, weights in results/model.json")
    print("Honesty  : each window's risk comes from a model that NEVER saw that run")
    print("           (5-fold cross-validation, split by run)")
    print("Decision : say YES (livelock coming) if risk >= %.2f" % threshold)
    print("")

    print("1. CONFUSION MATRIX (counting the %d windows into 4 boxes)" % total)
    print("")
    print("                               model said NO     model said YES")
    print("   really NO   (%4d windows)    TN = %4d          FP = %4d" % (tn + fp, tn, fp))
    print("   really YES  (%4d windows)    FN = %4d          TP = %4d" % (fn + tp, fn, tp))
    print("")
    print("   TP: livelock was coming and we warned      FN: livelock was coming, we missed it")
    print("   TN: nothing was coming and we stayed quiet FP: nothing was coming, we warned anyway")
    print("")

    print("2. METRICS (formula  ->  numbers from the matrix  ->  result)")
    print("")
    print("   Accuracy          = (TP + TN) / all       = (%d + %d) / %d  = %s"
          % (tp, tn, total, pct(m["accuracy"])))
    print("   Precision         = TP / (TP + FP)        = %d / %d           = %s"
          % (tp, tp + fp, pct(m["precision"])))
    print("   Recall            = TP / (TP + FN)        = %d / %d           = %s"
          % (tp, tp + fn, pct(m["recall"])))
    print("   Specificity       = TN / (TN + FP)        = %d / %d         = %s"
          % (tn, tn + fp, pct(m["specificity"])))
    print("   Balanced accuracy = (Recall + Spec.) / 2  = (%.3f + %.3f) / 2 = %s"
          % (m["recall"], m["specificity"], pct(m["balanced_accuracy"])))
    print("   F1 score          = 2PR / (P + R)         = 2 x %.3f x %.3f / %.3f = %s"
          % (m["precision"], m["recall"], m["precision"] + m["recall"], pct(m["f1"])))
    print("   MCC               = (TP.TN - FP.FN) / sqrt((TP+FP)(TP+FN)(TN+FP)(TN+FN))")
    print("                     = (%d x %d - %d x %d) / sqrt(%d x %d x %d x %d) = %.3f"
          % (tp, tn, fp, fn, tp + fp, tp + fn, tn + fp, tn + fn, m["mcc"]))
    print("                       (MCC runs from -1 to +1; 0 = guessing, 1 = perfect)")
    print("")

    auc, pairs = roc_auc_by_hand(y, risk)
    ap = pr_auc_by_hand(y, risk)
    print("3. RANKING QUALITY (no threshold needed)")
    print("")
    print("   ROC-AUC = share of (livelock, no-livelock) window pairs where the")
    print("             livelock window got the higher risk = %.3f   (%d pairs checked)" % (auc, pairs))
    print("   PR-AUC  = area under the precision-recall curve  = %.3f   (random guess: %.3f)"
          % (ap, divide(sum(y), len(y))))
    print("")

    always_no = metrics_from_cells(0, 0, tn + fp, fn + tp)
    rtp, rfp, rtn, rfn = count_cells(y, rule)
    r = metrics_from_cells(rtp, rfp, rtn, rfn)
    print("4. WHY WE DO NOT LOOK AT ACCURACY ALONE")
    print("")
    print("   Only %.0f%% of windows are YES, so a 'model' that always says NO is" % (100.0 * divide(sum(y), len(y))))
    print("   already %.1f%% accurate. Balanced accuracy, recall, F1 and MCC show the real gap:" % (100.0 * always_no["accuracy"]))
    print("")
    print("   %-36s %9s %9s %8s %8s %7s" % ("", "accuracy", "bal.acc", "recall", "F1", "MCC"))
    print("   %-36s %9s %9s %8s %8s %7.2f" % ("our model", pct(m["accuracy"]), pct(m["balanced_accuracy"]),
                                             pct(m["recall"]), pct(m["f1"]), m["mcc"]))
    print("   %-36s %9s %9s %8s %8s %7.2f" % ("simple rule (rho > 1 and overload)", pct(r["accuracy"]),
                                             pct(r["balanced_accuracy"]), pct(r["recall"]), pct(r["f1"]), r["mcc"]))
    print("   %-36s %9s %9s %8s %8s %7.2f" % ("always say NO", pct(always_no["accuracy"]),
                                             pct(always_no["balanced_accuracy"]), pct(always_no["recall"]),
                                             pct(always_no["f1"]), always_no["mcc"]))
    print("")
    print("SUMMARY: accuracy %.1f%%, balanced accuracy %.1f%%, recall %.1f%%, F1 %.1f%%, MCC %.2f"
          % (100 * m["accuracy"], 100 * m["balanced_accuracy"], 100 * m["recall"], 100 * m["f1"], m["mcc"]))
    print("(full evaluation with confidence intervals and run-time cost: python evaluate_model.py)")


if __name__ == "__main__":
    main()
