import argparse
import csv
import gzip
import io
import os
import sys
import tarfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from common.config import RESULTS_DIR, EDGES, edge_name, make_dirs
from common.telemetry import read_call_records, read_scenarios

# Compares the testbed with REAL production data: the Alibaba microservice
# trace (cluster-trace-microservices-v2021, about 20,000 microservices).
#
# Download ONE file, e.g. MSCallGraph_0.tar.gz (~170 MB) from
#   http://aliopentrace.oss-cn-beijing.aliyuncs.com/v2021MicroservicesTraces/MSCallGraph/MSCallGraph_0.tar.gz
# and run
#   python calibrate_from_trace.py MSCallGraph_0.tar.gz
# (.tar.gz, .csv.gz or an extracted .csv all work)
#
# What the trace CAN tell us: call-graph shape (fan-out, depth) and
# response times of real service-to-service calls.
# What it CANNOT: retries, error codes, livelock. It has no attempt number
# and no status field. That is why the dataset itself has to be generated,
# and this trace is used to check that the testbed is realistic.

REPORT_PATH = os.path.join(RESULTS_DIR, "calibration_report.txt")

UPSTREAM_NAMES = ["um", "upstream", "upstream_microservice"]
DOWNSTREAM_NAMES = ["dm", "downstream", "downstream_microservice"]
RT_NAMES = ["rt", "response_time", "responsetime"]
RPCID_NAMES = ["rpcid", "rpc_id"]
RPCTYPE_NAMES = ["rpctype", "rpc_type"]

SERVICE_CALL_TYPES = ["rpc", "http"]
MAX_ROWS = 2000000


def open_trace(path):
    # returns a text stream of the CSV, whatever the packaging
    if path.endswith(".tar.gz") or path.endswith(".tgz") or path.endswith(".tar"):
        archive = tarfile.open(path, "r:*")
        for member in archive:
            if member.isfile() and member.name.endswith(".csv"):
                print("reading %s from inside the archive" % member.name)
                raw = archive.extractfile(member)
                return io.TextIOWrapper(raw, encoding="utf-8", errors="replace")
        print("error: no .csv file inside %s" % path)
        return None
    if path.endswith(".gz"):
        return gzip.open(path, "rt", encoding="utf-8", errors="replace")
    return open(path, "r", encoding="utf-8", errors="replace")


def find_column(header, candidates):
    lowered = [h.strip().lower() for h in header]
    for name in candidates:
        if name in lowered:
            return lowered.index(name)
    return -1


def percentile(sorted_values, fraction):
    if len(sorted_values) == 0:
        return 0.0
    index = int(fraction * (len(sorted_values) - 1))
    return sorted_values[index]


def looks_like_header(row):
    lowered = [h.strip().lower() for h in row]
    return "um" in lowered or "dm" in lowered or "traceid" in lowered


def read_trace(path, max_rows):
    stream = open_trace(path)
    if stream is None:
        return None

    downstreams = {}
    depths = []
    response_times = []
    rows_read = 0
    rows_skipped = 0

    reader = csv.reader(stream)
    try:
        first = next(reader)
    except StopIteration:
        print("error: the file is empty")
        return None

    if looks_like_header(first):
        header = first
    else:
        # the v2021 files have no header row; this is the documented order
        header = ["", "traceid", "timestamp", "rpcid", "um", "rpctype", "dm", "interface", "rt"]
        if len(first) == 8:
            header = header[1:]
        print("no header row found, using the documented v2021 column order")

    i_um = find_column(header, UPSTREAM_NAMES)
    i_dm = find_column(header, DOWNSTREAM_NAMES)
    i_rt = find_column(header, RT_NAMES)
    i_rpc = find_column(header, RPCID_NAMES)
    i_type = find_column(header, RPCTYPE_NAMES)
    if i_um < 0 or i_dm < 0:
        print("error: could not find upstream/downstream columns in: %s" % header)
        return None

    pending = []
    if not looks_like_header(first):
        pending.append(first)

    while True:
        if len(pending) > 0:
            row = pending.pop()
        else:
            try:
                row = next(reader)
            except StopIteration:
                break
        rows_read = rows_read + 1
        if rows_read > max_rows:
            break
        if len(row) <= max(i_um, i_dm):
            rows_skipped = rows_skipped + 1
            continue

        if i_type >= 0 and i_type < len(row):
            if row[i_type].strip().lower() not in SERVICE_CALL_TYPES:
                continue

        upstream = row[i_um].strip()
        downstream = row[i_dm].strip()
        if upstream in ("", "NAN", "(?)") or downstream in ("", "NAN", "(?)"):
            rows_skipped = rows_skipped + 1
            continue

        if upstream not in downstreams:
            downstreams[upstream] = set()
        downstreams[upstream].add(downstream)

        if i_rt >= 0 and i_rt < len(row):
            try:
                # each RPC is logged twice (caller side and callee side) and
                # the sign tells which side; the size is the response time
                rt = abs(float(row[i_rt]))
                response_times.append(rt)
            except ValueError:
                pass

        if i_rpc >= 0 and i_rpc < len(row):
            rpcid = row[i_rpc].strip()
            if rpcid != "":
                depths.append(rpcid.count(".") + 1)

    stream.close()
    print("read %d rows (%d skipped for missing service names)" % (rows_read, rows_skipped))
    return downstreams, depths, response_times


def testbed_latencies():
    # measured healthy latency of each testbed edge, from the dataset runs
    ids = []
    windows = {}
    for manifest in read_scenarios():
        if manifest.get("group") == "dataset" and not manifest.get("interrupted"):
            ids.append(manifest["scenario_id"])
            for phase in manifest["phases"]:
                if phase["name"] == "normal":
                    windows[manifest["scenario_id"]] = (phase["start"], phase["end"])
                    break
    if len(ids) == 0:
        return {}
    records, skipped = read_call_records(ids)
    by_edge = {}
    for record in records:
        bounds = windows.get(record["scenario_id"])
        if bounds is None or not (bounds[0] <= record["timestamp"] < bounds[1]):
            continue
        if record["response_code"] != 200:
            continue
        name = record["source_service"] + "->" + record["dest_service"]
        if name not in by_edge:
            by_edge[name] = []
        by_edge[name].append(record["latency_ms"])
    result = {}
    for name in by_edge:
        values = sorted(by_edge[name])
        result[name] = (percentile(values, 0.5), percentile(values, 0.9), len(values))
    return result


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("trace", help="an MSCallGraph file from the Alibaba v2021 trace (.tar.gz, .csv.gz or .csv)")
    parser.add_argument("--max_rows", type=int, default=MAX_ROWS)
    args = parser.parse_args()
    make_dirs()

    if not os.path.exists(args.trace):
        print("error: no such file: %s" % args.trace)
        sys.exit(1)

    result = read_trace(args.trace, args.max_rows)
    if result is None:
        sys.exit(1)
    downstreams, depths, response_times = result
    if len(downstreams) == 0:
        print("error: no usable service-to-service rows found -- is this an MSCallGraph file?")
        sys.exit(1)

    fanouts = sorted([len(downstreams[s]) for s in downstreams])
    response_times.sort()
    depths.sort()

    lines = []
    lines.append("Testbed vs the Alibaba production microservice trace (v2021, MSCallGraph)")
    lines.append("=" * 74)
    lines.append("rows used: service-to-service calls (rpc/http) from %s" % os.path.basename(args.trace))
    lines.append("")
    lines.append("%-34s %18s %18s" % ("", "production", "our testbed"))
    lines.append("%-34s %18d %18d" % ("services that call others", len(downstreams), 2))
    lines.append("%-34s %18s %18s" % ("fan-out (median / p90 / max)",
                                     "%d / %d / %d" % (percentile(fanouts, 0.5), percentile(fanouts, 0.9), fanouts[-1]),
                                     "1-2 / 2 / 2"))
    lines.append("%-34s %18s %18s" % ("call depth (median / p90)",
                                     "%d / %d" % (percentile(depths, 0.5), percentile(depths, 0.9)), "2 / 2"))
    lines.append("%-34s %18s" % ("response time p50 / p90 / p99 (ms)",
                                 "%.1f / %.1f / %.1f" % (percentile(response_times, 0.5),
                                                         percentile(response_times, 0.9),
                                                         percentile(response_times, 0.99))))
    tb = testbed_latencies()
    if len(tb) > 0:
        lines.append("")
        lines.append("testbed healthy call latency, measured from the dataset runs (ms):")
        for edge in EDGES:
            name = edge_name(edge)
            if name in tb:
                p50, p90, n = tb[name]
                lines.append("  %-36s p50 %6.1f   p90 %6.1f   (%d calls)" % (name, p50, p90, n))
    lines.append("")
    if percentile(depths, 0.5) > 2:
        lines.append("Production call chains are deeper than ours. Retries multiply per layer")
        lines.append("(3 attempts x 3 layers = 27 calls), so a 2-layer testbed UNDER-states the")
        lines.append("amplification a real deep chain would suffer: our results are a lower bound.")
    else:
        lines.append("Our 2-layer chain matches the typical production depth in this sample.")
    lines.append("")
    lines.append("NOT in this trace: attempt numbers, status codes, livelock labels. It can")
    lines.append("calibrate the testbed's shape and timing, it cannot replace the generated data.")

    text = "\n".join(lines)
    print("")
    print(text)
    with open(REPORT_PATH, "w", encoding="utf-8") as f:
        f.write(text + "\n")
    print("")
    print("saved to %s" % REPORT_PATH)


if __name__ == "__main__":
    main()
