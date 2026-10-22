import argparse
import csv
import json
import os
import pickle
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import networkx as nx

from common.config import (CODE_DIR, DATA_DIR, RESULTS_DIR, EDGES, NODES, PARENT_EDGE,
                           CHILD_EDGES, edge_name, make_dirs)
from common.plots import plt, save_figure, BLUE, ORANGE, RED, GREY
from features import make_features, risk_probability


LABELED_PATH = os.path.join(DATA_DIR, "labeled_windows.csv")
SNAPSHOT_PATH = os.path.join(DATA_DIR, "graph_snapshots.pkl")
OOF_PATH = os.path.join(RESULTS_DIR, "prediction_oof.csv")
MODEL_PATH = os.path.join(RESULTS_DIR, "model.json")

NODE_POSITIONS = {
    "gateway": (0.0, 0.0),
    "order_service": (1.0, 0.0),
    "payment_service": (2.0, 0.55),
    "inventory_service": (2.0, -0.55),
}

STAT_FIELDS = ["n_calls", "lambda_base", "lambda_retry", "rho", "s_useful", "concurrency",
               "p_success", "fail_ratio", "overload_ratio", "mean_latency_ms"]


def to_number(text):
    if text == "" or text is None:
        return None
    return float(text)


def load_rows(path):
    with open(path, "r") as f:
        return list(csv.DictReader(f))


def load_oof_risk():
    risk = {}
    if not os.path.exists(OOF_PATH):
        return risk
    with open(OOF_PATH, "r") as f:
        for row in csv.DictReader(f):
            risk[(row["scenario_id"], row["edge"], int(row["window_index"]))] = float(row["risk"])
    return risk


def build_snapshots(rows, oof_risk, model):
    by_scenario = {}
    for row in rows:
        scenario_id = row["scenario_id"]
        index = int(row["window_index"])
        edge = (row["source_service"], row["dest_service"])
        if scenario_id not in by_scenario:
            by_scenario[scenario_id] = {}
        if index not in by_scenario[scenario_id]:
            by_scenario[scenario_id][index] = {}
        stats = {}
        for field in STAT_FIELDS:
            stats[field] = to_number(row[field])
        stats["n_calls"] = int(stats["n_calls"])
        stats["label"] = row["label"]
        stats["phase"] = row["phase"]
        stats["t_rel"] = float(row["t_rel"])
        by_scenario[scenario_id][index][edge] = stats

    snapshots = {}
    for scenario_id in sorted(by_scenario):
        windows = by_scenario[scenario_id]
        snapshots[scenario_id] = []
        for index in sorted(windows):
            stats_now = windows[index]
            graph = nx.DiGraph()
            graph.add_nodes_from(NODES)

            risks = {}
            if model is not None:
                values = make_features(stats_now, windows.get(index - 1, {}), model["baselines"])
            for edge in EDGES:
                key = (scenario_id, edge_name(edge), index)
                if key in oof_risk:
                    risks[edge] = oof_risk[key]
                elif model is not None and edge in stats_now:
                    risks[edge] = risk_probability(model, values[edge])
                else:
                    risks[edge] = None

            phase = "unknown"
            t_rel = index * 2.0
            for edge in EDGES:
                if edge not in stats_now:
                    continue
                s = stats_now[edge]
                phase = s["phase"]
                t_rel = s["t_rel"]
                graph.add_edge(edge[0], edge[1],
                               rho=s["rho"], lambda_base=s["lambda_base"],
                               lambda_retry=s["lambda_retry"], s_useful=s["s_useful"],
                               concurrency=s["concurrency"], p_success=s["p_success"],
                               livelock=(s["label"] == "livelock"), risk=risks[edge])

            for node in NODES:
                out_retry = 0.0
                in_retry = 0.0
                for u, v, d in graph.out_edges(node, data=True):
                    out_retry = out_retry + d["lambda_retry"]
                for u, v, d in graph.in_edges(node, data=True):
                    in_retry = in_retry + d["lambda_retry"]
                graph.nodes[node]["out_retry_rate"] = out_retry
                graph.nodes[node]["in_retry_rate"] = in_retry

            snapshots[scenario_id].append({"window_index": index, "t_rel": t_rel,
                                           "phase": phase, "graph": graph})
    return snapshots


def edge_colour(data, threshold):
    if data["livelock"]:
        return RED
    if data["risk"] is not None and data["risk"] >= threshold:
        return ORANGE
    return GREY


def draw(ax, snapshot, title, threshold):
    graph = snapshot["graph"]
    colours = []
    widths = []
    labels = {}
    for u, v, d in graph.edges(data=True):
        colours.append(edge_colour(d, threshold))
        widths.append(2.0 + min(d["rho"], 4.0))
        text = "rho %.2f" % d["rho"]
        if d["risk"] is not None:
            text = text + "\nrisk %.2f" % d["risk"]
        labels[(u, v)] = text

    nx.draw_networkx_nodes(graph, NODE_POSITIONS, ax=ax, node_size=2600, node_color=BLUE)
    short = {"gateway": "gateway", "order_service": "order", "payment_service": "payment",
             "inventory_service": "inventory"}
    nx.draw_networkx_labels(graph, NODE_POSITIONS, labels=short, ax=ax, font_color="white", font_size=9)
    nx.draw_networkx_edges(graph, NODE_POSITIONS, ax=ax, edge_color=colours, width=widths,
                           arrowsize=18, node_size=2600)
    nx.draw_networkx_edge_labels(graph, NODE_POSITIONS, edge_labels=labels, ax=ax, font_size=8)
    ax.set_title(title)
    ax.set_xlim(-0.4, 2.4)
    ax.set_ylim(-0.95, 0.95)
    ax.axis("off")


def pick_three(snaps, threshold=0.5):
    healthy = None
    warning = None
    livelock = None
    for i in range(len(snaps)):
        snap = snaps[i]
        any_livelock = False
        any_risk = False
        for u, v, d in snap["graph"].edges(data=True):
            if d["livelock"]:
                any_livelock = True
            if d["risk"] is not None and d["risk"] >= threshold:
                any_risk = True
        if healthy is None and snap["phase"] == "normal" and not any_risk and not any_livelock:
            healthy = snap
        if livelock is None and any_livelock:
            livelock = snap
            for j in range(i):
                earlier = snaps[j]
                warned = False
                locked = False
                for u, v, d in earlier["graph"].edges(data=True):
                    if d["risk"] is not None and d["risk"] >= threshold:
                        warned = True
                    if d["livelock"]:
                        locked = True
                if warned and not locked and earlier["phase"] != "normal":
                    warning = earlier
                    break
            if warning is None and i > 0:
                warning = snaps[i - 1]
    return healthy, warning, livelock


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--labels", type=str, default=LABELED_PATH)
    parser.add_argument("--scenario", type=str, default=None)
    parser.add_argument("--no_show", action="store_true")
    args = parser.parse_args()
    make_dirs()

    if not os.path.exists(args.labels):
        print("error: %s not found. Run labeling.py first." % args.labels)
        sys.exit(1)

    model = None
    if os.path.exists(MODEL_PATH):
        with open(MODEL_PATH, "r") as f:
            model = json.load(f)

    rows = load_rows(args.labels)
    snapshots = build_snapshots(rows, load_oof_risk(), model)
    threshold = 0.5
    if model is not None:
        threshold = model["threshold"]
    total = 0
    for scenario_id in snapshots:
        total = total + len(snapshots[scenario_id])
    with open(SNAPSHOT_PATH, "wb") as f:
        pickle.dump(snapshots, f)

    print("SERVICE GRAPH  |  %d runs  |  %d snapshots (one per 2 s window)  |  %s"
          % (len(snapshots), total, os.path.relpath(SNAPSHOT_PATH, CODE_DIR)))
    print("")
    print("Table 1. Message passing used by the model")
    header = "%-20s %-36s %s" % ("Edge", "Edges below", "Edge above")
    print(header)
    print("-" * len(header))
    for edge in EDGES:
        below = [short(e) for e in CHILD_EDGES[edge]]
        above = PARENT_EDGE[edge]
        print("%-20s %-36s %s" % (short(edge), ", ".join(below) if below else "-",
                                  short(above) if above else "-"))

    scenario_id = args.scenario
    if scenario_id is None:
        best = -1
        for sid in sorted(snapshots):
            count = 0
            for snap in snapshots[sid]:
                for u, v, d in snap["graph"].edges(data=True):
                    if d["livelock"]:
                        count = count + 1
            if count > best:
                best = count
                scenario_id = sid
    if scenario_id not in snapshots:
        print("error: run %s not found" % scenario_id)
        sys.exit(1)

    healthy, warning, livelock = pick_three(snapshots[scenario_id], threshold)
    if livelock is None:
        print("run %s has no livelock window" % scenario_id)
        return

    panels = [(healthy, "Healthy"), (warning, "Early warning"), (livelock, "Livelock")]
    print("")
    print("Table 2. Snapshots of %s (risk threshold %.2f)" % (scenario_id, threshold))
    header = "%-14s %6s %-9s %-18s %6s %6s %9s" % ("Snapshot", "t (s)", "Phase", "Edge", "rho", "Risk", "Livelock")
    print(header)
    print("-" * len(header))
    for snap, name in panels:
        if snap is None:
            continue
        for u, v, d in snap["graph"].edges(data=True):
            risk = "-" if d["risk"] is None else "%.2f" % d["risk"]
            print("%-14s %6.1f %-9s %-18s %6.2f %6s %9s" % (name, snap["t_rel"], snap["phase"], short((u, v)),
                                                          d["rho"], risk, "yes" if d["livelock"] else "no"))
            name = ""

    fig, axes = plt.subplots(1, 3, figsize=(15, 4.4))
    for k in range(3):
        snap, name = panels[k]
        if snap is None:
            axes[k].axis("off")
            continue
        draw(axes[k], snap, "(%s) %s, t = %.0f s" % ("abc"[k], name, snap["t_rel"]), threshold)
    fig.suptitle("%s: grey = normal, orange = risk >= %.2f, red = livelock" % (scenario_id, threshold),
                 fontsize=11, x=0.02, ha="left")
    fig.tight_layout()
    print("")
    save_figure(fig, "graph_snapshots.png", not args.no_show)


def short(edge):
    return edge_name(edge).replace("_service", "")


if __name__ == "__main__":
    main()
