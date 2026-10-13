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
                      reactive_alarm, WINDOW_SEC)

# Module 7: the retry-budget controller.
#
# Every second, for every edge, it measures the last 2 seconds of calls and
# decides whether to CUT the retry budget, HOLD it, or RESTORE it.
#
# It works like TCP congestion control, with "overload" (503 busy replies
# and timeouts) as the congestion signal:
#
# When to cut (two modes):
#   reactive    a storm is happening right now: rho > 1 (more retries than
#               fresh calls) AND the callee is overloaded (>20% of calls
#               get 503 or time out)
#   predictive  the model says livelock is likely within the next ~10s AND
#               congestion is starting below this edge (the callee's response
#               time has at least doubled, or >=10% overload), OR the reactive
#               rule fires (kept as a safety net). The congestion gate matters:
#               without it the model also cut retries during pure error bursts,
#               where the callee fails fast and has free slots, so a retry is
#               a useful second chance (see README, "lessons").
#
# How to cut: max_attempts - 1 (never below 1) and backoff x2 (at most x4).
# Path rule: retries multiply down a call chain (3 x 3 = 9 calls), so when
# an edge is cut, the edge ABOVE it on the path is cut too.
#
# When to restore: NOT when rho is low. Once max_attempts is 1, rho is 0 by
# definition (no retries are allowed), so "rho is low, give retries back"
# undoes the cut while the callee is still drowning -> on/off flapping.
# (The Review-2 controller had exactly this bug.) Instead we restore when
# the edge has been free of overload for RESTORE_TICKS checks in a row, and
# at least HOLD_SEC after the last cut -- one step at a time. Overload is
# measured on fresh calls too, so it is honest even with retries off.
#
# Errors without overload (the callee says 500 but has free slots) do not
# block a restore: then a retry is a fresh chance to succeed and costs
# capacity nobody else needs. Cut fast on congestion, give back slowly when
# it clears -- the same shape as TCP's AIMD.

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
    # Is the callee side of this edge filling up? Overload (503s/timeouts)
    # on this edge or below it, or the callee's response time at least
    # doubled. For an edge whose callee calls others (gateway->order), we
    # look at the edges below it: order_service's own response time also
    # contains its retry back-off sleeps, which is not congestion.
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
    # "no congestion": almost no 503s or timeouts on this edge
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
    # one decision round. Kept separate from the loop so it can be unit tested.
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


def short_name(edge):
    names = {"gateway": "gw", "order_service": "order", "payment_service": "pay",
             "inventory_service": "inv"}
    return names[edge[0]] + "->" + names[edge[1]]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", type=str, default="reactive", choices=["reactive", "predictive"])
    parser.add_argument("--duration_sec", type=float, default=60)
    parser.add_argument("--model", type=str, default=DEFAULT_MODEL_PATH)
    parser.add_argument("--tag", type=str, default="manual",
                        help="label written into controller_log.jsonl (usually the scenario id)")
    parser.add_argument("--keep_policy", action="store_true",
                        help="do not put the original retry policy back when stopping")
    parser.add_argument("--quiet", action="store_true")
    args = parser.parse_args()

    model = None
    if args.mode == "predictive":
        model = load_model(args.model)
        if model is None:
            print("error: predictive mode needs a trained model at %s" % args.model)
            print("run prediction_model.py first, or use --mode reactive")
            sys.exit(1)

    original_policy = read_policy_file()
    state_by_edge = starting_state()
    tail = make_tail_state(start_at_end=True)
    recent = []

    if not args.quiet:
        print("controller (%s) running for %.0f sec, checking every %.1f sec"
              % (args.mode, args.duration_sec, CONTROL_INTERVAL_SEC))

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
            # never leave the services stuck with retries switched off
            write_policy_file(original_policy)
        if not args.quiet:
            print("controller stopped%s." % ("" if args.keep_policy else ", original retry policy restored"))


if __name__ == "__main__":
    main()
