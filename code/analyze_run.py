import argparse
import math
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from common.config import DEFAULT_SLOTS
from common.telemetry import (read_call_records, read_user_records, read_server_records,
                              read_scenarios, within_run)
from common.plots import plt, save_figure, mark_phases, BLUE, ORANGE, GREEN, RED, PURPLE

BUCKET_SEC = 2.0
LAST_SEC = 5.0


def phase_of(manifest, t):
    for phase in manifest["phases"]:
        if phase["start"] <= t < phase["end"]:
            return phase["name"]
    if t >= manifest["phases"][-1]["end"]:
        return "after_end"
    return "before_start"


def find_manifest(manifests, scenario_id):
    found = None
    for m in manifests:
        if m["scenario_id"] == scenario_id:
            if found is None or not m.get("interrupted"):
                found = m
    return found


def phase_table(manifest, users, calls, servers):
    order = []
    for phase in manifest["phases"]:
        if phase["name"] not in order:
            order.append(phase["name"])

    stats = {}
    for name in order:
        stats[name] = {"users": 0, "ok": 0, "time": 0.0, "calls": 0, "retries": 0,
                       "work": 0, "orphaned": 0, "seconds": 0.0, "start": None, "end": None}
    for phase in manifest["phases"]:
        s = stats[phase["name"]]
        s["seconds"] = s["seconds"] + phase["end"] - phase["start"]
        if s["start"] is None:
            s["start"] = phase["start"] - manifest["start"]
        s["end"] = phase["end"] - manifest["start"]

    for record in users:
        name = phase_of(manifest, record["start_time"])
        if name in stats:
            stats[name]["users"] = stats[name]["users"] + 1
            stats[name]["time"] = stats[name]["time"] + record["total_time_ms"]
            if record["succeeded"]:
                stats[name]["ok"] = stats[name]["ok"] + 1

    for record in calls:
        name = phase_of(manifest, record.get("start_time", record["timestamp"]))
        if name in stats:
            stats[name]["calls"] = stats[name]["calls"] + 1
            if record["attempt_number"] > 1:
                stats[name]["retries"] = stats[name]["retries"] + 1

    for record in servers:
        if record["service"] != "order_service" or record["status"] == 503:
            continue
        name = phase_of(manifest, record["slot_time"])
        if name in stats:
            stats[name]["work"] = stats[name]["work"] + 1
            if record["orphaned"]:
                stats[name]["orphaned"] = stats[name]["orphaned"] + 1

    rows = []
    for name in order:
        s = stats[name]
        rows.append({
            "phase": name,
            "span": "%.0f-%.0f" % (s["start"], s["end"]),
            "users": s["users"],
            "success": 100.0 * s["ok"] / s["users"] if s["users"] > 0 else 0.0,
            "goodput": s["ok"] / s["seconds"] if s["seconds"] > 0 else 0.0,
            "latency": s["time"] / s["users"] if s["users"] > 0 else 0.0,
            "calls": s["calls"],
            "retry": 100.0 * s["retries"] / s["calls"] if s["calls"] > 0 else 0.0,
            "per_success": float(s["calls"]) / s["ok"] if s["ok"] > 0 else float(s["calls"]),
            "orphaned": 100.0 * s["orphaned"] / s["work"] if s["work"] > 0 else 0.0,
        })
    return rows


def last_seconds(manifest, users):
    end = manifest["phases"][-1]["end"]
    total = 0
    ok = 0
    for record in users:
        if end - LAST_SEC <= record["start_time"] < end:
            total = total + 1
            if record["succeeded"]:
                ok = ok + 1
    return ok, total


def timeline(manifest, users, calls, servers):
    t0 = manifest["start"]
    n = int(math.ceil((manifest["end"] - t0) / BUCKET_SEC))
    xs = [k * BUCKET_SEC + BUCKET_SEC / 2.0 for k in range(n)]
    user_total = [0] * n
    user_ok = [0] * n
    fresh = [0] * n
    retries = [0] * n
    busy = [0.0] * n
    done = [0] * n
    orphaned = [0] * n

    for record in users:
        k = int(math.floor((record["start_time"] - t0) / BUCKET_SEC))
        if 0 <= k < n:
            user_total[k] = user_total[k] + 1
            if record["succeeded"]:
                user_ok[k] = user_ok[k] + 1

    for record in calls:
        k = int(math.floor((record.get("start_time", record["timestamp"]) - t0) / BUCKET_SEC))
        if 0 <= k < n:
            if record["attempt_number"] == 1:
                fresh[k] = fresh[k] + 1
            else:
                retries[k] = retries[k] + 1

    slots = DEFAULT_SLOTS["order_service"]
    for record in servers:
        if record["service"] != "order_service" or record["status"] == 503:
            continue
        start = record["slot_time"]
        end = record["timestamp"]
        first = max(int(math.floor((start - t0) / BUCKET_SEC)), 0)
        last = min(int(math.floor((end - t0) / BUCKET_SEC)), n - 1)
        for k in range(first, last + 1):
            w = t0 + k * BUCKET_SEC
            overlap = min(end, w + BUCKET_SEC) - max(start, w)
            if overlap > 0:
                busy[k] = busy[k] + overlap
        k = int(math.floor((start - t0) / BUCKET_SEC))
        if 0 <= k < n:
            done[k] = done[k] + 1
            if record["orphaned"]:
                orphaned[k] = orphaned[k] + 1

    result = {"x": xs, "served_x": [], "served": [], "orphan_x": [], "orphan": []}
    for k in range(n):
        if user_total[k] > 0:
            result["served_x"].append(xs[k])
            result["served"].append(100.0 * user_ok[k] / user_total[k])
        if done[k] > 0:
            result["orphan_x"].append(xs[k])
            result["orphan"].append(100.0 * orphaned[k] / done[k])
    result["fresh"] = [v / BUCKET_SEC for v in fresh]
    result["retries"] = [v / BUCKET_SEC for v in retries]
    result["busy"] = [min(100.0 * b / (slots * BUCKET_SEC), 100.0) for b in busy]
    return result


def draw(manifest, series, show):
    fig, axes = plt.subplots(3, 1, figsize=(9, 7.2), sharex=True)
    for ax in axes:
        mark_phases(ax, manifest)
    mark_phases(axes[0], manifest, label=True)

    axes[0].plot(series["served_x"], series["served"], color=BLUE, marker="o", markersize=3)
    axes[0].set_ylim(-5, 105)
    axes[0].set_ylabel("Users served (%)")
    axes[0].set_title("(a) Users served", pad=18)

    axes[1].stackplot(series["x"], series["fresh"], series["retries"], colors=[GREEN, ORANGE], alpha=0.85,
                      labels=["First attempts", "Retries"])
    axes[1].set_ylabel("Calls / s")
    axes[1].set_title("(b) Internal calls")
    axes[1].legend(loc="upper left")

    axes[2].plot(series["x"], series["busy"], color=PURPLE, label="Slots busy")
    axes[2].plot(series["orphan_x"], series["orphan"], color=RED, label="Orphaned work")
    axes[2].set_ylim(-5, 105)
    axes[2].set_ylabel("order_service (%)")
    axes[2].set_xlabel("Time (s)")
    axes[2].set_title("(c) order_service")
    axes[2].legend(loc="upper left")
    axes[2].set_xlim(0, manifest["end"] - manifest["start"])

    fig.suptitle("Run %s: %s %s fault on %s, %.0f users/s, slot wait %.0f s"
                 % (manifest["scenario_id"], manifest["mode"], manifest["fault"], manifest["target"],
                    manifest["rps"], manifest["queue_wait"]), fontsize=12, fontweight="bold", x=0.02,
                 ha="left")
    fig.tight_layout()
    return save_figure(fig, "run_%s.png" % manifest["scenario_id"], show)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("scenario_id", nargs="?", default=None)
    parser.add_argument("--no_show", action="store_true")
    args = parser.parse_args()

    manifests = read_scenarios()
    if len(manifests) == 0:
        print("error: no runs recorded, run scenario_runner.py first")
        sys.exit(1)
    if args.scenario_id is None:
        manifest = manifests[-1]
    else:
        manifest = find_manifest(manifests, args.scenario_id)
        if manifest is None:
            print("error: no run called '%s'" % args.scenario_id)
            sys.exit(1)
    scenario_id = manifest["scenario_id"]

    users, s1 = read_user_records([scenario_id])
    calls, s2 = read_call_records([scenario_id])
    servers, s3 = read_server_records([scenario_id])
    users = within_run(users, manifest)
    calls = within_run(calls, manifest)
    servers = within_run(servers, manifest)

    print("RUN %s  |  %s %s fault on %s  |  %.0f users/s  |  order slot wait %.0f s"
          % (scenario_id, manifest["mode"], manifest["fault"], manifest["target"],
             manifest["rps"], manifest["queue_wait"]))
    print("")
    header = "%-9s %8s %6s %8s %9s %9s %7s %8s %9s %9s" % (
        "Phase", "Time(s)", "Users", "Served%", "Goodput/s", "Latency", "Calls", "Retry%",
        "Calls/OK", "Orphan%")
    print(header)
    print("-" * len(header))
    for r in phase_table(manifest, users, calls, servers):
        print("%-9s %8s %6d %8.1f %9.1f %7.0fms %7d %8.1f %9.1f %9.1f" % (
            r["phase"], r["span"], r["users"], r["success"], r["goodput"], r["latency"], r["calls"],
            r["retry"], r["per_success"], r["orphaned"]))
    ok, total = last_seconds(manifest, users)
    print("")
    if total > 0:
        print("Last %.0f s: %d/%d users served (%.1f%%)" % (LAST_SEC, ok, total, 100.0 * ok / total))
    draw(manifest, timeline(manifest, users, calls, servers), not args.no_show)


if __name__ == "__main__":
    main()
