import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from common.telemetry import (read_call_records, read_user_records, read_server_records,
                              read_scenarios, within_run)

# Looks at ONE scenario run and prints, phase by phase, how much useful
# output users got compared to how hard the system worked. The gap between
# the two is the livelock.
#
#   python analyze_run.py demo1      (a specific scenario)
#   python analyze_run.py            (the most recent scenario)


def phase_of(manifest, t):
    for phase in manifest["phases"]:
        if phase["start"] <= t < phase["end"]:
            return phase["name"]
    if t >= manifest["phases"][-1]["end"]:
        return "after_end"
    return "before_start"


def main():
    manifests = read_scenarios()
    if len(manifests) == 0:
        print("no scenarios recorded yet. Run scenario_runner.py first.")
        sys.exit(1)

    if len(sys.argv) > 1:
        scenario_id = sys.argv[1]
        manifest = None
        for m in manifests:
            if m["scenario_id"] == scenario_id:
                manifest = m
        if manifest is None:
            print("no scenario called '%s'. Recorded scenarios:" % scenario_id)
            for m in manifests[-15:]:
                print("  " + m["scenario_id"])
            sys.exit(1)
    else:
        manifest = manifests[-1]
        scenario_id = manifest["scenario_id"]

    users, s1 = read_user_records([scenario_id])
    calls, s2 = read_call_records([scenario_id])
    servers, s3 = read_server_records([scenario_id])
    users = within_run(users, manifest)
    calls = within_run(calls, manifest)
    servers = within_run(servers, manifest)
    if s1 + s2 + s3 > 0:
        print("warning: skipped %d damaged log line(s)" % (s1 + s2 + s3))

    print("scenario %s: %s %s fault on %s, %.0f users/s, order queue %.0fs"
          % (scenario_id, manifest["mode"], manifest["fault"], manifest["target"],
             manifest["rps"], manifest["queue_wait"]))
    print("")

    order = []
    for phase in manifest["phases"]:
        if phase["name"] not in order:
            order.append(phase["name"])

    stats = {}
    for name in order:
        stats[name] = {"users": 0, "ok": 0, "time": 0.0, "calls": 0, "retries": 0,
                       "work": 0, "orphaned": 0, "seconds": 0.0}
    for phase in manifest["phases"]:
        stats[phase["name"]]["seconds"] = stats[phase["name"]]["seconds"] + phase["end"] - phase["start"]

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

    print("%-10s %8s %9s %10s %12s %8s %7s %11s %10s" % (
        "phase", "users", "success%", "goodput/s", "avg latency", "calls", "retry%",
        "calls/succ", "orphaned%"))
    print("-" * 94)
    for name in order:
        s = stats[name]
        success = 100.0 * s["ok"] / s["users"] if s["users"] > 0 else 0.0
        goodput = s["ok"] / s["seconds"] if s["seconds"] > 0 else 0.0
        latency = s["time"] / s["users"] if s["users"] > 0 else 0.0
        retry_pct = 100.0 * s["retries"] / s["calls"] if s["calls"] > 0 else 0.0
        per_success = float(s["calls"]) / s["ok"] if s["ok"] > 0 else float(s["calls"])
        orphaned = 100.0 * s["orphaned"] / s["work"] if s["work"] > 0 else 0.0
        print("%-10s %8d %8.1f%% %10.1f %10.0fms %8d %6.0f%% %11.1f %9.1f%%" % (
            name, s["users"], success, goodput, latency, s["calls"], retry_pct, per_success, orphaned))

    # the recovery row is an AVERAGE over the whole recovery phase, which
    # includes the time spent draining the backlog. The last few seconds
    # show where the system ended up.
    end = manifest["phases"][-1]["end"]
    last_users = 0
    last_ok = 0
    for record in users:
        if end - 5.0 <= record["start_time"] < end:
            last_users = last_users + 1
            if record["succeeded"]:
                last_ok = last_ok + 1
    if last_users > 0:
        print("")
        print("users in the LAST 5 seconds of the run who got a success: %.0f%% (%d of %d)"
              % (100.0 * last_ok / last_users, last_ok, last_users))

    print("")
    print("calls      = every internal call attempt (how hard the system worked)")
    print("goodput    = successful user requests per second (what users actually got)")
    print("orphaned%  = share of order_service's finished work that finished AFTER the")
    print("             gateway had already given up on it (busy for nobody)")


if __name__ == "__main__":
    main()
