import argparse
import math
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from common.config import RESULTS_DIR, DEFAULT_SLOTS, DEFAULT_SERVICE_CONFIGS, make_dirs
from common.telemetry import (read_call_records, read_user_records, read_server_records,
                              read_scenarios, within_run)

# "Why should anyone trust a dataset you generated yourself?"
#
# Because it can be checked. This script tests the generated logs against
# things that MUST be true if the testbed behaves like a real system:
#
#  1. nothing is lost: every user request, every gateway call and every
#     downstream call is accounted for (exact bookkeeping identities)
#  2. retries follow the rules: attempts are numbered 1,2,3 in order and
#     never exceed the policy in force
#  3. queueing theory: a service with c slots can never be more than 100%
#     busy (Utilization Law, U = X * S / c <= 1), and when it saturates its
#     throughput equals c / (mean service time)
#  4. the fault injector does what it was told (measured latency / error
#     rate during the fault matches the configured values)
#  5. the load generator sends the rate it was asked to
#  6. timestamps are consistent (no call ends before it starts)
#
# Results go to results/data_validation.txt as PASS / FAIL lines.

WINDOW_SEC = 2.0


def check(lines, ok, text):
    lines.append("  [%s] %s" % ("PASS" if ok else "FAIL", text))
    return ok


def group_by(records, key):
    grouped = {}
    for record in records:
        value = record.get(key)
        if value not in grouped:
            grouped[value] = []
        grouped[value].append(record)
    return grouped


def phase_bounds(manifest, name):
    for phase in manifest["phases"]:
        if phase["name"] == name:
            return phase["start"], phase["end"]
    return None


def bookkeeping(lines, manifests, users_by, calls_by, servers_by):
    lines.append("1. Bookkeeping -- nothing lost, nothing invented")
    all_ok = True
    worst_user_gap = 0
    worst_edge_gap = 0
    for manifest in manifests:
        sid = manifest["scenario_id"]
        users = users_by.get(sid, [])
        calls = calls_by.get(sid, [])
        servers = servers_by.get(sid, [])

        # every request the load generator sent was logged as a user request
        gap = abs(manifest["requests_sent"] - len(users))
        if gap > worst_user_gap:
            worst_user_gap = gap

        gateway_received = 0
        order_did_downstream = 0
        for record in servers:
            if record["service"] == "gateway":
                gateway_received = gateway_received + 1
            if record["service"] == "order_service" and record["status"] in (200, 502):
                order_did_downstream = order_did_downstream + 1

        first_calls = {"order_service": 0, "payment_service": 0, "inventory_service": 0}
        for record in calls:
            if record["attempt_number"] == 1:
                first_calls[record["dest_service"]] = first_calls[record["dest_service"]] + 1

        # each request the gateway received made exactly ONE first attempt
        # to order_service; each order that did its own work successfully
        # made exactly one first attempt to payment and one to inventory
        gaps = [abs(gateway_received - first_calls["order_service"]),
                abs(order_did_downstream - first_calls["payment_service"]),
                abs(order_did_downstream - first_calls["inventory_service"])]
        for g in gaps:
            if g > worst_edge_gap:
                worst_edge_gap = g

    all_ok = check(lines, worst_user_gap == 0,
                   "user requests sent == user requests logged, in every scenario (largest gap: %d)"
                   % worst_user_gap) and all_ok
    all_ok = check(lines, worst_edge_gap == 0,
                   "requests received by a service == first attempts its caller logged (largest gap: %d)"
                   % worst_edge_gap) and all_ok
    return all_ok


def retry_sequences(lines, calls):
    lines.append("2. Retry rules -- attempts numbered 1,2,3... and never above the policy limit")
    by_call = {}
    missing_ids = 0
    for record in calls:
        call_id = record.get("call_id")
        if call_id is None:
            missing_ids = missing_ids + 1
            continue
        if call_id not in by_call:
            by_call[call_id] = []
        by_call[call_id].append(record)

    if len(by_call) == 0:
        check(lines, False, "no call_id field in the logs (data recorded by an older version)")
        return False

    bad_order = 0
    over_limit = 0
    retried_success = 0
    total = 0
    pattern = re.compile(r"_max_(\d+)_")
    for call_id in by_call:
        attempts = by_call[call_id]
        attempts.sort(key=attempt_key)
        total = total + 1
        for k in range(len(attempts)):
            if attempts[k]["attempt_number"] != k + 1:
                bad_order = bad_order + 1
                break
        match = pattern.search(attempts[0]["retry_policy"])
        if match is not None and len(attempts) > int(match.group(1)):
            over_limit = over_limit + 1
        for k in range(len(attempts) - 1):
            if attempts[k]["response_code"] == 200:
                retried_success = retried_success + 1
                break

    ok = True
    ok = check(lines, bad_order == 0, "%d logical calls checked: attempts always numbered 1..k in order (%d broken)"
               % (total, bad_order)) and ok
    ok = check(lines, over_limit == 0, "no call made more attempts than its policy allowed (%d violations)"
               % over_limit) and ok
    ok = check(lines, retried_success == 0, "a call is never retried after it succeeded (%d violations)"
               % retried_success) and ok
    return ok


def attempt_key(record):
    return record["attempt_number"]


def utilization(lines, manifests, servers_by):
    lines.append("3. Queueing theory -- Utilization Law U = X*S/c <= 1, saturation throughput = c/S")
    worst_u = 0.0
    table = []
    for manifest in manifests:
        if manifest.get("mode") == "none":
            continue
        sid = manifest["scenario_id"]
        target = manifest["target"]
        if target not in DEFAULT_SLOTS:
            continue
        c = DEFAULT_SLOTS[target]
        served = []
        for record in servers_by.get(sid, []):
            if record["service"] == target and record["status"] != 503:
                served.append(record)

        # busy slot-seconds in each 2s window (exact overlap)
        t0 = manifest["start"]
        n_windows = int(math.ceil((manifest["end"] - t0) / WINDOW_SEC))
        busy = [0.0] * max(n_windows, 1)
        for record in served:
            start = record["slot_time"]
            end = record["timestamp"]
            first = int(math.floor((start - t0) / WINDOW_SEC))
            last = int(math.floor((end - t0) / WINDOW_SEC))
            for k in range(max(first, 0), min(last, n_windows - 1) + 1):
                w_start = t0 + k * WINDOW_SEC
                overlap = min(end, w_start + WINDOW_SEC) - max(start, w_start)
                if overlap > 0:
                    busy[k] = busy[k] + overlap
        for k in range(n_windows):
            u = busy[k] / (c * WINDOW_SEC)
            if u > worst_u:
                worst_u = u

        fault = phase_bounds(manifest, "fault")
        if fault is None or manifest["fault"] != "latency":
            continue
        in_fault = []
        for record in served:
            if fault[0] <= record["slot_time"] and record["timestamp"] < fault[1]:
                in_fault.append(record)
        if len(in_fault) < 5:
            continue
        mean_service = sum([r["service_ms"] for r in in_fault]) / len(in_fault) / 1000.0
        capacity = c / mean_service
        seconds = fault[1] - fault[0]
        measured = len(in_fault) / seconds
        u_fault = 0.0
        for k in range(n_windows):
            w_start = t0 + k * WINDOW_SEC
            if fault[0] <= w_start and w_start + WINDOW_SEC <= fault[1]:
                u_fault = max(u_fault, busy[k] / (c * WINDOW_SEC))
        table.append((sid, target, c, mean_service * 1000, capacity, measured, u_fault))

    ok = check(lines, worst_u <= 1.02,
               "no 2s window ever had a service more than 100%% busy (highest: %.1f%%)" % (100 * worst_u))
    if len(table) > 0:
        lines.append("     latency-fault scenarios, during the held fault:")
        lines.append("     %-30s %-18s %2s %12s %16s %16s %12s" % ("scenario", "service", "c", "service time",
                                                                  "theory c/S", "measured", "peak busy"))
        ratios = []
        for sid, target, c, s_ms, capacity, measured, u_fault in table:
            lines.append("     %-30s %-18s %2d %10.0fms %13.2f/s %13.2f/s %11.0f%%" % (
                sid, target, c, s_ms, capacity, measured, 100 * u_fault))
            ratios.append(measured / capacity)
        mean_ratio = sum(ratios) / len(ratios)
        ok = check(lines, 0.75 <= mean_ratio <= 1.05,
                   "under the held fault, measured throughput is %.0f%% of the c/S limit on average "
                   "(the service is saturated: busy, not idle)" % (100 * mean_ratio)) and ok
    return ok


def fault_fidelity(lines, manifests, servers_by):
    lines.append("4. Fault injection -- the service really behaved as configured")
    # A measured average always wobbles around the true value just by
    # chance, by about one "standard error". So the fair test is: is the
    # measurement within a few standard errors of what was configured?
    #   latency: drawn uniformly from a 200 ms wide range -> std = 200/sqrt(12)
    #   errors:  each request fails with probability p   -> std = sqrt(p(1-p))
    # and the standard error of an average of n values is std / sqrt(n).
    latency_z = []
    latency_bias = []
    error_z = []
    for manifest in manifests:
        if manifest.get("mode") == "none":
            continue
        fault = phase_bounds(manifest, "fault")
        if fault is None:
            continue
        sid = manifest["scenario_id"]
        target = manifest["target"]
        if target == "order_service":
            continue
        served = []
        for record in servers_by.get(sid, []):
            if record["service"] == target and record["status"] != 503:
                if fault[0] + 0.5 <= record["slot_time"] and record["timestamp"] < fault[1]:
                    served.append(record)
        n = len(served)
        if n < 5:
            continue
        if manifest["fault"] == "latency":
            measured = sum([r["service_ms"] for r in served]) / n
            configured = manifest["peak_latency_ms"]
            standard_error = (200.0 / math.sqrt(12.0)) / math.sqrt(n)
            latency_z.append(abs(measured - configured) / standard_error)
            latency_bias.append((measured - configured) / configured)
        else:
            failed = len([r for r in served if r["status"] == 500])
            measured = float(failed) / n
            p = manifest["peak_failure_rate"]
            standard_error = math.sqrt(p * (1 - p) / n)
            error_z.append(abs(measured - p) / standard_error)

    ok = True
    if len(latency_z) > 0:
        bias = 100.0 * sum(latency_bias) / len(latency_bias)
        ok = check(lines, max(latency_z) < 3.5,
                   "latency faults: every run's measured service time is within %.1f standard errors of the"
                   " configured value (%d runs; average bias %+.1f%%, Python timer + logging overhead)"
                   % (max(latency_z), len(latency_z), bias)) and ok
    if len(error_z) > 0:
        ok = check(lines, max(error_z) < 3.5,
                   "error faults: every run's measured failure fraction is within %.1f standard errors of the"
                   " configured rate (%d runs)" % (max(error_z), len(error_z))) and ok

    normal_fail = []
    for manifest in manifests:
        normal = phase_bounds(manifest, "normal")
        sid = manifest["scenario_id"]
        for service in ["payment_service", "inventory_service"]:
            served = []
            for record in servers_by.get(sid, []):
                if record["service"] == service and record["status"] != 503:
                    if normal[0] <= record["slot_time"] < normal[1]:
                        served.append(record)
            if len(served) >= 20:
                failed = len([r for r in served if r["status"] == 500])
                normal_fail.append(float(failed) / len(served))
    if len(normal_fail) > 0:
        mean_fail = sum(normal_fail) / len(normal_fail)
        expected = DEFAULT_SERVICE_CONFIGS["payment_service"]["failure_rate"]
        ok = check(lines, abs(mean_fail - expected) < 0.01,
                   "healthy leaf services fail %.1f%% of requests on average (configured %.0f%%)"
                   % (100 * mean_fail, 100 * expected)) and ok
    return ok


def load_fidelity(lines, manifests, users_by):
    lines.append("5. Load generator -- users arrive at the configured rate")
    errors = []
    for manifest in manifests:
        users = users_by.get(manifest["scenario_id"], [])
        seconds = manifest["end"] - manifest["start"]
        if seconds <= 0 or len(users) == 0:
            continue
        measured = len(users) / seconds
        errors.append(abs(measured - manifest["rps"]) / manifest["rps"])
    if len(errors) == 0:
        return check(lines, False, "no scenarios to check")
    mean_error = sum(errors) / len(errors)
    return check(lines, mean_error < 0.10,
                 "achieved arrival rate within %.1f%% of the target on average (worst %.1f%%, random Poisson arrivals)"
                 % (100 * mean_error, 100 * max(errors)))


def time_sanity(lines, calls, users, servers):
    lines.append("6. Timestamps -- nothing ends before it starts")
    bad = 0
    for record in calls:
        if record.get("start_time", record["timestamp"]) > record["timestamp"] + 1e-6:
            bad = bad + 1
    for record in users:
        if record["start_time"] > record["timestamp"] + 1e-6:
            bad = bad + 1
    for record in servers:
        if not (record["arrived"] <= record["slot_time"] + 1e-6 <= record["timestamp"] + 2e-6):
            bad = bad + 1
    return check(lines, bad == 0, "%d records checked, %d with impossible times"
                 % (len(calls) + len(users) + len(servers), bad))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--group", type=str, default="all")
    args = parser.parse_args()
    make_dirs()

    manifests = []
    for manifest in read_scenarios():
        if manifest.get("interrupted"):
            continue
        if args.group == "all" or manifest.get("group") == args.group:
            manifests.append(manifest)
    if len(manifests) == 0:
        print("no scenarios found. Run some scenarios first.")
        sys.exit(1)

    ids = [m["scenario_id"] for m in manifests]
    calls, s1 = read_call_records(ids)
    users, s2 = read_user_records(ids)
    servers, s3 = read_server_records(ids)

    lines = []
    lines.append("Data validation -- %d scenarios, %d call attempts, %d user requests, %d server records"
                 % (len(manifests), len(calls), len(users), len(servers)))
    lines.append("=" * 78)
    if s1 + s2 + s3 > 0:
        lines.append("note: %d damaged log line(s) skipped" % (s1 + s2 + s3))

    users_by = group_by(users, "scenario_id")
    calls_by = group_by(calls, "scenario_id")
    servers_by = group_by(servers, "scenario_id")
    for manifest in manifests:
        sid = manifest["scenario_id"]
        users_by[sid] = within_run(users_by.get(sid, []), manifest)
        calls_by[sid] = within_run(calls_by.get(sid, []), manifest)
        servers_by[sid] = within_run(servers_by.get(sid, []), manifest)
    calls = []
    users = []
    servers = []
    for manifest in manifests:
        calls.extend(calls_by[manifest["scenario_id"]])
        users.extend(users_by[manifest["scenario_id"]])
        servers.extend(servers_by[manifest["scenario_id"]])

    results = []
    results.append(bookkeeping(lines, manifests, users_by, calls_by, servers_by))
    results.append(retry_sequences(lines, calls))
    results.append(utilization(lines, manifests, servers_by))
    results.append(fault_fidelity(lines, manifests, servers_by))
    results.append(load_fidelity(lines, manifests, users_by))
    results.append(time_sanity(lines, calls, users, servers))

    lines.append("")
    passed = len([r for r in results if r])
    lines.append("%d of %d check groups passed" % (passed, len(results)))

    text = "\n".join(lines)
    print(text)
    with open(os.path.join(RESULTS_DIR, "data_validation.txt"), "w") as f:
        f.write(text + "\n")


if __name__ == "__main__":
    main()
