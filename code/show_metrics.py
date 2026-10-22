import argparse
import csv
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from common.config import RESULTS_DIR
from matplotlib.colors import ListedColormap

from common.plots import plt, save_figure, BLUE, ORANGE, GREY

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
                "alarm": int(row["alarm"]),
                "threshold": float(row["threshold"]),
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


def roc_points(y, scores):
    pairs = []
    for i in range(len(y)):
        pairs.append((scores[i], y[i]))
    pairs.sort(key=risk_of, reverse=True)
    total_yes = sum(y)
    total_no = len(y) - total_yes
    fpr = [0.0]
    tpr = [0.0]
    tp = 0
    fp = 0
    i = 0
    while i < len(pairs):
        j = i
        while j < len(pairs) and pairs[j][0] == pairs[i][0]:
            if pairs[j][1] == 1:
                tp = tp + 1
            else:
                fp = fp + 1
            j = j + 1
        fpr.append(divide(fp, total_no))
        tpr.append(divide(tp, total_yes))
        i = j
    return fpr, tpr


def pr_points(y, scores):
    pairs = []
    for i in range(len(y)):
        pairs.append((scores[i], y[i]))
    pairs.sort(key=risk_of, reverse=True)
    total_yes = sum(y)
    recall = []
    precision = []
    found = 0
    seen = 0
    i = 0
    while i < len(pairs):
        j = i
        while j < len(pairs) and pairs[j][0] == pairs[i][0]:
            seen = seen + 1
            found = found + pairs[j][1]
            j = j + 1
        recall.append(divide(found, total_yes))
        precision.append(divide(found, seen))
        i = j
    return recall, precision


def pct(x):
    return "%.1f%%" % (100.0 * x)


def draw(cells, y, risk, m, auc, ap, show):
    tp, fp, tn, fn = cells
    fig, axes = plt.subplots(1, 3, figsize=(13, 4.3))

    ax = axes[0]
    grid = [[tn, fp], [fn, tp]]
    ax.imshow([[1, 0], [0, 1]], cmap=ListedColormap(["#fbd5d1", "#cfeacb"]), vmin=0, vmax=1)
    names = [["TN", "FP"], ["FN", "TP"]]
    for r in range(2):
        for c in range(2):
            ax.text(c, r, "%s\n%d" % (names[r][c], grid[r][c]), ha="center", va="center", fontsize=13,
                    fontweight="bold")
    ax.set_xticks([0, 1])
    ax.set_xticklabels(["Predicted NO", "Predicted YES"])
    ax.set_yticks([0, 1])
    ax.set_yticklabels(["Actual NO", "Actual YES"])
    ax.grid(False)
    for side in ["top", "right", "left", "bottom"]:
        ax.spines[side].set_visible(False)
    ax.set_title("(a) Confusion matrix, accuracy %s" % pct(m["accuracy"]))

    fpr, tpr = roc_points(y, risk)
    axes[1].plot(fpr, tpr, color=BLUE, label="Logistic regression (AUC %.3f)" % auc)
    axes[1].plot([0, 1], [0, 1], color=GREY, linestyle="--", linewidth=1, label="Random (AUC 0.5)")
    axes[1].plot([divide(fp, fp + tn)], [m["recall"]], "o", color=ORANGE, markersize=8, label="Operating point")
    axes[1].set_xlabel("False positive rate")
    axes[1].set_ylabel("True positive rate (recall)")
    axes[1].set_title("(b) ROC curve")
    axes[1].legend(loc="lower right")

    recall, precision = pr_points(y, risk)
    axes[2].plot(recall, precision, color=BLUE, label="Logistic regression (AP %.3f)" % ap)
    axes[2].axhline(divide(sum(y), len(y)), color=GREY, linestyle="--", linewidth=1,
                    label="Random (%.3f)" % divide(sum(y), len(y)))
    axes[2].plot([m["recall"]], [m["precision"]], "o", color=ORANGE, markersize=8, label="Operating point")
    axes[2].set_xlabel("Recall")
    axes[2].set_ylabel("Precision")
    axes[2].set_ylim(0, 1.03)
    axes[2].set_title("(c) Precision-recall curve")
    axes[2].legend(loc="lower left", bbox_to_anchor=(0.0, 0.2))
    fig.tight_layout()
    return save_figure(fig, "metrics.png", show)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--no_show", action="store_true")
    args = parser.parse_args()
    if not os.path.exists(OOF_PATH):
        print("error: %s not found, run prediction_model.py first" % OOF_PATH)
        sys.exit(1)

    rows = load_rows()
    y = []
    risk = []
    pred = []
    rule = []
    thresholds = set()
    runs = set()
    for r in rows:
        y.append(r["y"])
        risk.append(r["risk"])
        pred.append(r["alarm"])
        rule.append(r["rule"])
        thresholds.add(r["threshold"])
        runs.add(r["scenario_id"])

    tp, fp, tn, fn = count_cells(y, pred)
    total = tp + fp + tn + fn
    m = metrics_from_cells(tp, fp, tn, fn)
    auc, pairs = roc_auc_by_hand(y, risk)
    ap = pr_auc_by_hand(y, risk)

    print("PREDICTION METRICS  |  %d out-of-fold windows from %d runs  |  threshold %.2f-%.2f (nested CV)"
          % (total, len(runs), min(thresholds), max(thresholds)))
    print("")
    print("Table 1. Confusion matrix")
    header = "%-14s %14s %14s %10s" % ("", "Predicted NO", "Predicted YES", "Total")
    print(header)
    print("-" * len(header))
    print("%-14s %14s %14s %10d" % ("Actual NO", "TN = %d" % tn, "FP = %d" % fp, tn + fp))
    print("%-14s %14s %14s %10d" % ("Actual YES", "FN = %d" % fn, "TP = %d" % tp, fn + tp))
    print("%-14s %14d %14d %10d" % ("Total", tn + fn, fp + tp, total))
    print("")

    print("Table 2. Metrics")
    header = "%-18s %-26s %-44s %9s" % ("Metric", "Formula", "Computation", "Value")
    print(header)
    print("-" * len(header))
    print("%-18s %-26s %-44s %9s" % ("Accuracy", "(TP+TN) / N", "(%d+%d) / %d" % (tp, tn, total),
                                      pct(m["accuracy"])))
    print("%-18s %-26s %-44s %9s" % ("Precision", "TP / (TP+FP)", "%d / %d" % (tp, tp + fp), pct(m["precision"])))
    print("%-18s %-26s %-44s %9s" % ("Recall", "TP / (TP+FN)", "%d / %d" % (tp, tp + fn), pct(m["recall"])))
    print("%-18s %-26s %-44s %9s" % ("Specificity", "TN / (TN+FP)", "%d / %d" % (tn, tn + fp),
                                      pct(m["specificity"])))
    print("%-18s %-26s %-44s %9s" % ("Balanced accuracy", "(Recall+Specificity) / 2",
                                      "(%.3f+%.3f) / 2" % (m["recall"], m["specificity"]),
                                      pct(m["balanced_accuracy"])))
    print("%-18s %-26s %-44s %9s" % ("F1", "2PR / (P+R)",
                                      "2(%.3f)(%.3f) / %.3f" % (m["precision"], m["recall"],
                                                                m["precision"] + m["recall"]), pct(m["f1"])))
    print("%-18s %-26s %-44s %9.3f" % ("MCC", "(TP.TN-FP.FN) / sqrt(...)",
                                        "(%dx%d-%dx%d) / sqrt(%dx%dx%dx%d)" % (tp, tn, fp, fn, tp + fp, tp + fn,
                                                                                tn + fp, tn + fn), m["mcc"]))
    print("%-18s %-26s %-44s %9.3f" % ("ROC-AUC", "P(risk+ > risk-)", "%d pairs" % pairs, auc))
    print("%-18s %-26s %-44s %9.3f" % ("PR-AUC", "sum dRecall x Precision", "%d windows" % total, ap))
    print("")

    always_no = metrics_from_cells(0, 0, tn + fp, fn + tp)
    rtp, rfp, rtn, rfn = count_cells(y, rule)
    r = metrics_from_cells(rtp, rfp, rtn, rfn)
    print("Table 3. Comparison")
    header = "%-22s %8s %8s %8s %8s %8s %8s" % ("Method", "Acc", "BalAcc", "Prec", "Recall", "F1", "MCC")
    print(header)
    print("-" * len(header))
    for name, x in [("Logistic regression", m), ("Reactive rule", r), ("Always NO", always_no)]:
        print("%-22s %8s %8s %8s %8s %8s %8.3f" % (name, pct(x["accuracy"]), pct(x["balanced_accuracy"]),
                                                    pct(x["precision"]), pct(x["recall"]), pct(x["f1"]), x["mcc"]))
    print("")
    draw((tp, fp, tn, fn), y, risk, m, auc, ap, not args.no_show)


if __name__ == "__main__":
    main()
