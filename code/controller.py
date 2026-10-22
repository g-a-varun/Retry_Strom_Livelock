import argparse
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from common.config import (EDGES, PARENT_EDGE, CHILD_EDGES, RESULTS_DIR, edge_name,
                           DEFAULT_MAX_ATTEMPTS, DEFAULT_BACKOFF_BASE_MS, DEFAULT_TIMEOUT_MS)
from common.retry_policy import read_policy_file, write_policy_file, get_retry_policy
from common.telemetry import make_tail_state, read_new_call_records, log_controller_decision
from features import (live_window_stats, split_by_edge, make_features, risk_probability,
                      reactive_alarm, WINDOW_SEC, ALARM_RHO)
from common.plots import plt, save_figure, BLUE, RED, GREEN, GREY


CONTROL_INTERVAL_SEC = 1.0

MIN_CALLS = 3
OVERLOAD_OK = 0.1
CONGESTION_LATENCY = 2.0
CONGESTION_OVERLOAD = 0.1
RESTORE_TICKS = 3
HOLD_SEC = 4.0
CUT_COOLDOWN_SEC = 2.0

MIN_MAX_ATTEMPTS = 1
MAX_BACKOFF_MULTIPLIER = 4.0

DEFAULT_MODEL_PATH = os.path.join(RESULTS_DIR, "model.json")


def load_model(path):
    if not os.path.exists(path):
        return None
    with open(path, "r") as f:
        return json.load(f)


def starting_state():
    state = {}
    for edge in EDGES:
        policy = get_retry_policy(edge_name(edge))
        start_attempts = policy["max_attempts"]
        if start_attempts < 1:
            start_attempts = DEFAULT_MAX_ATTEMPTS
        state[edge] = {
            "max_attempts": start_attempts,
            "starting_max_attempts": start_attempts,
            "backoff_base_ms": policy["backoff_base_ms"],
            "backoff_multiplier": 1.0,
            "timeout_ms": policy["timeout_ms"],
            "last_cut": 0.0,
            "healthy_ticks": 0,
        }
    return state


def congestion_starting(edge, values):
    if values["overload_ratio"] >= CONGESTION_OVERLOAD:
        return True
    if values["down_max_overload"] >= CONGESTION_OVERLOAD:
        return True
    if len(CHILD_EDGES[edge]) == 0:
        return values["latency_ratio"] >= CONGESTION_LATENCY
    return values["down_max_latency_ratio"] >= CONGESTION_LATENCY


def decide_risky(mode, edge, stats, prob, values, model):
    if stats["n_calls"] < MIN_CALLS:
        return False
    reactive = reactive_alarm(stats)
    if mode == "reactive":
        return reactive
    predicted = prob is not None and prob >= model["threshold"] and congestion_starting(edge, values)
    return reactive or predicted


def looks_healthy(stats):
    if stats["n_calls"] < MIN_CALLS:
        return False
    return stats["overload_ratio"] < OVERLOAD_OK


def cut(state, now):
    changed = False
    if state["max_attempts"] > MIN_MAX_ATTEMPTS:
        state["max_attempts"] = state["max_attempts"] - 1
        changed = True
    if state["backoff_multiplier"] < MAX_BACKOFF_MULTIPLIER:
        state["backoff_multiplier"] = min(MAX_BACKOFF_MULTIPLIER, state["backoff_multiplier"] * 2.0)
        changed = True
    state["last_cut"] = now
    state["healthy_ticks"] = 0
    return changed


def restore(state):
    changed = False
    if state["max_attempts"] < state["starting_max_attempts"]:
        state["max_attempts"] = state["max_attempts"] + 1
        changed = True
    if state["backoff_multiplier"] > 1.0:
        state["backoff_multiplier"] = max(1.0, state["backoff_multiplier"] / 2.0)
        changed = True
    state["healthy_ticks"] = 0
    return changed


def control_step(mode, model, state_by_edge, stats_now, stats_prev, now):
    probs = {}
    values_by_edge = {}
    if mode == "predictive":
        values_by_edge = make_features(stats_now, stats_prev, model["baselines"])
        for edge in EDGES:
            probs[edge] = risk_probability(model, values_by_edge[edge])
    else:
        for edge in EDGES:
            probs[edge] = None
            values_by_edge[edge] = None

    risky = {}
    healthy = {}
    for edge in EDGES:
        risky[edge] = decide_risky(mode, edge, stats_now[edge], probs[edge], values_by_edge[edge], model)
        healthy[edge] = looks_healthy(stats_now[edge])

    to_cut = set()
    for edge in EDGES:
        if risky[edge]:
            to_cut.add(edge)
            parent = PARENT_EDGE[edge]
            if parent is not None:
                to_cut.add(parent)

    actions = {}
    changed = False
    for edge in EDGES:
        state = state_by_edge[edge]
        action = "hold"
        if edge in to_cut:
            if now - state["last_cut"] >= CUT_COOLDOWN_SEC:
                if cut(state, now):
                    action = "cut"
                    changed = True
        else:
            if healthy[edge]:
                state["healthy_ticks"] = state["healthy_ticks"] + 1
            else:
                state["healthy_ticks"] = 0
            below_start = (state["max_attempts"] < state["starting_max_attempts"]
                           or state["backoff_multiplier"] > 1.0)
            if (below_start and state["healthy_ticks"] >= RESTORE_TICKS
                    and now - state["last_cut"] >= HOLD_SEC):
                if restore(state):
                    action = "restore"
                    changed = True
        actions[edge] = action

    return probs, risky, healthy, actions, changed


def write_policy(state_by_edge):
    policy = read_policy_file()
    for edge in EDGES:
        state = state_by_edge[edge]
        name = edge_name(edge)
        budget_ratio = None
        if name in policy and isinstance(policy[name], dict):
            budget_ratio = policy[name].get("budget_ratio")
        policy[name] = {
            "max_attempts": state["max_attempts"],
            "backoff_base_ms": int(state["backoff_base_ms"] * state["backoff_multiplier"]),
            "timeout_ms": state["timeout_ms"],
            "budget_ratio": budget_ratio,
        }
    return write_policy_file(policy)


def print_summary(history, restored):
    print("")
    header = "%-14s %6s %9s %14s %10s" % ("Edge", "Cuts", "Restores", "Min attempts", "First cut")
    print(header)
    print("-" * len(header))
    for edge in EDGES:
        rows = [h for h in history if h["edge"] == edge]
        cuts = [h for h in rows if h["action"] == "cut"]
        restores = [h for h in rows if h["action"] == "restore"]
        lowest = "-"
        if len(rows) > 0:
            lowest = str(min([h["attempts"] for h in rows]))
        first = "-"
        if len(cuts) > 0:
            first = "t=%.0fs" % cuts[0]["t"]
        print("%-14s %6d %9d %14s %10s" % (short_name(edge), len(cuts), len(restores), lowest, first))
    if restored:
        print("Retry policy restored to the original.")


def short_name(edge):
    names = {"gateway": "gw", "order_service": "order", "payment_service": "pay",
             "inventory_service": "inv"}
    return names[edge[0]] + "->" + names[edge[1]]


def draw(history, mode, threshold, name, show):
    fig, axes = plt.subplots(3, 1, figsize=(9, 7.5), sharex=True)
    colours = [BLUE, RED, GREEN]
    for k in range(len(EDGES)):
        edge = EDGES[k]
        rows = [h for h in history if h["edge"] == edge]
        if len(rows) == 0:
            continue
        xs = [h["t"] for h in rows]
        label = short_name(edge)
        axes[0].step(xs, [h["attempts"] for h in rows], where="post", color=colours[k], label=label)
        cuts = [h for h in rows if h["action"] == "cut"]
        restores = [h for h in rows if h["action"] == "restore"]
        axes[0].plot([h["t"] for h in cuts], [h["attempts"] for h in cuts], "v", color=colours[k], markersize=8)
        axes[0].plot([h["t"] for h in restores], [h["attempts"] for h in restores], "^", color=colours[k],
                     markersize=8)
        axes[1].plot(xs, [h["rho"] for h in rows], color=colours[k], label=label)
        risk_rows = [h for h in rows if h["risk"] is not None]
        axes[2].plot([h["t"] for h in risk_rows], [h["risk"] for h in risk_rows], color=colours[k], label=label)
    axes[0].set_yticks([1, 2, 3])
    axes[0].set_ylim(0.6, 3.4)
    axes[0].set_ylabel("Max attempts")
    axes[0].set_title("(a) Retry budget per edge (down = cut, up = restore)")
    axes[0].legend(loc="center right", ncol=3)
    axes[1].axhline(ALARM_RHO, color=GREY, linestyle="--", linewidth=1)
    axes[1].set_ylabel("rho")
    axes[1].set_title("(b) Retry amplification")
    if mode == "predictive":
        axes[2].axhline(threshold, color=GREY, linestyle="--", linewidth=1)
        axes[2].set_ylim(-0.05, 1.05)
        axes[2].set_title("(c) Predicted livelock risk (dashed: threshold %.2f)" % threshold)
    else:
        axes[2].set_title("(c) Predicted livelock risk (not used in reactive mode)")
    axes[2].set_ylabel("Risk p")
    axes[2].set_xlabel("Time since controller start (s)")
    fig.tight_layout()
    return save_figure(fig, name, show)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", type=str, default="reactive", choices=["reactive", "predictive"])
    parser.add_argument("--duration_sec", type=float, default=60)
    parser.add_argument("--model", type=str, default=DEFAULT_MODEL_PATH)
    parser.add_argument("--tag", type=str, default="manual")
    parser.add_argument("--keep_policy", action="store_true")
    parser.add_argument("--quiet", action="store_true")
    parser.add_argument("--no_show", action="store_true")
    args = parser.parse_args()

    model = None
    if args.mode == "predictive":
        model = load_model(args.model)
        if model is None:
            print("error: no model at %s, run prediction_model.py first" % args.model)
            sys.exit(1)

    original_policy = read_policy_file()
    state_by_edge = starting_state()
    tail = make_tail_state(start_at_end=True)
    recent = []
    history = []
    threshold = None
    if model is not None:
        threshold = model["threshold"]

    if not args.quiet:
        line = "CONTROLLER  |  mode %s  |  %.0f s  |  every %.0f s" % (args.mode, args.duration_sec,
                                                                      CONTROL_INTERVAL_SEC)
        if threshold is not None:
            line = line + "  |  risk threshold %.2f" % threshold
        print(line)

    start = time.time()
    end_time = start + args.duration_sec
    next_tick = start + CONTROL_INTERVAL_SEC
    try:
        while time.time() < end_time:
            delay = next_tick - time.time()
            if delay > 0:
                time.sleep(delay)
            next_tick = next_tick + CONTROL_INTERVAL_SEC
            now = time.time()

            recent.extend(read_new_call_records(tail))
            kept = []
            for record in recent:
                if record["timestamp"] > now - 2 * WINDOW_SEC:
                    kept.append(record)
            recent = kept

            by_edge = split_by_edge(recent)
            stats_now = {}
            stats_prev = {}
            for edge in EDGES:
                stats_now[edge] = live_window_stats(by_edge[edge], now)
                stats_prev[edge] = live_window_stats(by_edge[edge], now - WINDOW_SEC)

            probs, risky, healthy, actions, changed = control_step(
                args.mode, model, state_by_edge, stats_now, stats_prev, now)

            if changed:
                write_policy(state_by_edge)

            line = "t=+%4.0fs " % (now - start)
            for edge in EDGES:
                state = state_by_edge[edge]
                stats = stats_now[edge]
                log_controller_decision({
                    "timestamp": now,
                    "tag": args.tag,
                    "mode": args.mode,
                    "edge": edge_name(edge),
                    "n_calls": stats["n_calls"],
                    "rho": round(stats["rho"], 4),
                    "fail_ratio": round(stats["fail_ratio"], 4),
                    "overload_ratio": round(stats["overload_ratio"], 4),
                    "risk": None if probs[edge] is None else round(probs[edge], 4),
                    "risky": risky[edge],
                    "healthy": healthy[edge],
                    "action": actions[edge],
                    "max_attempts": state["max_attempts"],
                    "backoff_base_ms": int(state["backoff_base_ms"] * state["backoff_multiplier"]),
                })
                history.append({"t": now - start, "edge": edge, "attempts": state["max_attempts"],
                                "rho": stats["rho"], "risk": probs[edge], "action": actions[edge]})
                risk_text = ""
                if probs[edge] is not None:
                    risk_text = " risk %.2f" % probs[edge]
                mark = ""
                if actions[edge] == "cut":
                    mark = " CUT"
                elif actions[edge] == "restore":
                    mark = " RESTORE"
                line = line + "| %s rho %.2f%s -> %d%s " % (short_name(edge), stats["rho"], risk_text,
                                                           state["max_attempts"], mark)
            if not args.quiet:
                print(line)
    except KeyboardInterrupt:
        pass
    finally:
        if not args.keep_policy:
            write_policy_file(original_policy)
        if not args.quiet:
            print_summary(history, not args.keep_policy)
            if len(history) > 0:
                name = args.tag
                if name == "manual":
                    name = args.mode
                draw(history, args.mode, threshold, "controller_%s.png" % name, not args.no_show)


if __name__ == "__main__":
    main()
