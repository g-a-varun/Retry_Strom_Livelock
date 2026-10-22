import math

from common.config import EDGES, PARENT_EDGE, CHILD_EDGES, edge_name


WINDOW_SEC = 2.0
RHO_MAX = 10.0
RATIO_MAX = 50.0
USEFUL_MAX = 5.0
LOG_FLOOR = 0.01


def empty_counts():
    return {
        "n_first": 0,
        "n_retry": 0,
        "n_success": 0,
        "n_first_fail": 0,
        "n_busy": 0,
        "n_timeout": 0,
        "latency_sum": 0.0,
        "busy_time": 0.0,
    }


def record_start(record):
    if "start_time" in record:
        return record["start_time"]
    return record["timestamp"] - record["latency_ms"] / 1000.0


def add_record(counts, record):
    if record["attempt_number"] == 1:
        counts["n_first"] = counts["n_first"] + 1
        if record["response_code"] != 200:
            counts["n_first_fail"] = counts["n_first_fail"] + 1
    else:
        counts["n_retry"] = counts["n_retry"] + 1

    code = record["response_code"]
    if code == 200:
        counts["n_success"] = counts["n_success"] + 1
    elif code == 503:
        counts["n_busy"] = counts["n_busy"] + 1
    elif code == 0:
        counts["n_timeout"] = counts["n_timeout"] + 1

    counts["latency_sum"] = counts["latency_sum"] + record["latency_ms"]


def finish_window(counts, window_sec):
    n_calls = counts["n_first"] + counts["n_retry"]

    lambda_base = counts["n_first"] / window_sec
    lambda_retry = counts["n_retry"] / window_sec

    if counts["n_first"] > 0:
        rho = float(counts["n_retry"]) / counts["n_first"]
        if rho > RHO_MAX:
            rho = RHO_MAX
    elif counts["n_retry"] > 0:
        rho = RHO_MAX
    else:
        rho = 0.0

    if n_calls > 0:
        p_success = float(counts["n_success"]) / n_calls
        overload_ratio = float(counts["n_busy"] + counts["n_timeout"]) / n_calls
        mean_latency_ms = counts["latency_sum"] / n_calls
    else:
        p_success = None
        overload_ratio = 0.0
        mean_latency_ms = None

    if counts["n_first"] > 0:
        fail_ratio = float(counts["n_first_fail"]) / counts["n_first"]
    else:
        fail_ratio = 0.0

    return {
        "n_calls": n_calls,
        "lambda_base": lambda_base,
        "lambda_retry": lambda_retry,
        "rho": rho,
        "s_useful": counts["n_success"] / window_sec,
        "concurrency": counts["busy_time"] / window_sec,
        "p_success": p_success,
        "fail_ratio": fail_ratio,
        "overload_ratio": overload_ratio,
        "mean_latency_ms": mean_latency_ms,
    }


def window_stats_for_edge(records, t0, n_windows, window_sec=WINDOW_SEC):
    windows = []
    for i in range(n_windows):
        windows.append(empty_counts())

    for record in records:
        end = record["timestamp"]
        start = record_start(record)

        index = int(math.floor((end - t0) / window_sec))
        if 0 <= index < n_windows:
            add_record(windows[index], record)

        first = int(math.floor((start - t0) / window_sec))
        last = index
        if first < 0:
            first = 0
        if last > n_windows - 1:
            last = n_windows - 1
        k = first
        while k <= last:
            w_start = t0 + k * window_sec
            w_end = w_start + window_sec
            overlap = min(end, w_end) - max(start, w_start)
            if overlap > 0:
                windows[k]["busy_time"] = windows[k]["busy_time"] + overlap
            k = k + 1

    stats = []
    for counts in windows:
        stats.append(finish_window(counts, window_sec))
    return stats


def live_window_stats(records, window_end, window_sec=WINDOW_SEC):
    t0 = window_end - window_sec
    return window_stats_for_edge(records, t0, 1, window_sec)[0]


def split_by_edge(records):
    by_edge = {}
    for edge in EDGES:
        by_edge[edge] = []
    for record in records:
        edge = (record["source_service"], record["dest_service"])
        if edge in by_edge:
            by_edge[edge].append(record)
    return by_edge


ALARM_RHO = 1.0
ALARM_OVERLOAD = 0.2
ALARM_MIN_CALLS = 3


def reactive_alarm(stats):
    if stats is None or stats["n_calls"] < ALARM_MIN_CALLS:
        return False
    return stats["rho"] > ALARM_RHO and stats["overload_ratio"] > ALARM_OVERLOAD


FEATURE_NAMES = [
    "rho", "fail_ratio", "overload_ratio", "log_latency_ratio", "log_concurrency_ratio", "useful_ratio",
    "d_rho", "d_fail_ratio", "d_latency_ratio", "d_concurrency_ratio",
    "down_max_overload", "log_down_max_latency_ratio", "down_max_fail_ratio",
    "up_rho", "up_overload_ratio",
    "edge_gateway_order", "edge_order_payment", "edge_order_inventory",
]

TEMPORAL_FEATURES = ["d_rho", "d_fail_ratio", "d_latency_ratio", "d_concurrency_ratio"]
GRAPH_FEATURES = ["down_max_overload", "log_down_max_latency_ratio", "down_max_fail_ratio",
                  "up_rho", "up_overload_ratio"]


def capped(value):
    if value > RATIO_MAX:
        return RATIO_MAX
    return value


def latency_ratio(stats, edge, baselines):
    if stats is None or stats["mean_latency_ms"] is None:
        return 1.0
    base = baselines[edge_name(edge)]["latency_ms"]
    if base <= 0:
        return 1.0
    return capped(stats["mean_latency_ms"] / base)


def concurrency_ratio(stats, edge, baselines):
    if stats is None:
        return 1.0
    base = baselines[edge_name(edge)]["concurrency"]
    if base <= 0:
        return 1.0
    return capped(stats["concurrency"] / base)


def useful_ratio(stats, edge, baselines):
    if stats is None or stats["n_calls"] == 0:
        return 1.0
    base = baselines[edge_name(edge)].get("s_useful", 0.0)
    if base <= 0:
        return 1.0
    return min(stats["s_useful"] / base, USEFUL_MAX)


def log_ratio(value):
    return math.log(max(value, LOG_FLOOR))


def own_values(stats, edge, baselines):
    if stats is None or stats["n_calls"] == 0:
        return {"rho": 0.0, "fail_ratio": 0.0, "overload_ratio": 0.0,
                "latency_ratio": 1.0, "concurrency_ratio": concurrency_ratio(stats, edge, baselines),
                "useful_ratio": 1.0}
    return {
        "rho": stats["rho"],
        "fail_ratio": stats["fail_ratio"],
        "overload_ratio": stats["overload_ratio"],
        "latency_ratio": latency_ratio(stats, edge, baselines),
        "concurrency_ratio": concurrency_ratio(stats, edge, baselines),
        "useful_ratio": useful_ratio(stats, edge, baselines),
    }


def make_features(stats_now, stats_prev, baselines):
    own_now = {}
    own_prev = {}
    for edge in EDGES:
        own_now[edge] = own_values(stats_now.get(edge), edge, baselines)
        own_prev[edge] = own_values(stats_prev.get(edge), edge, baselines)

    result = {}
    for edge in EDGES:
        values = dict(own_now[edge])

        values["d_rho"] = own_now[edge]["rho"] - own_prev[edge]["rho"]
        values["d_fail_ratio"] = own_now[edge]["fail_ratio"] - own_prev[edge]["fail_ratio"]
        values["d_latency_ratio"] = own_now[edge]["latency_ratio"] - own_prev[edge]["latency_ratio"]
        values["d_concurrency_ratio"] = own_now[edge]["concurrency_ratio"] - own_prev[edge]["concurrency_ratio"]
        values["log_latency_ratio"] = log_ratio(own_now[edge]["latency_ratio"])
        values["log_concurrency_ratio"] = log_ratio(own_now[edge]["concurrency_ratio"])

        down_overload = 0.0
        down_latency = 1.0
        down_fail = 0.0
        for child in CHILD_EDGES[edge]:
            if own_now[child]["overload_ratio"] > down_overload:
                down_overload = own_now[child]["overload_ratio"]
            if own_now[child]["latency_ratio"] > down_latency:
                down_latency = own_now[child]["latency_ratio"]
            if own_now[child]["fail_ratio"] > down_fail:
                down_fail = own_now[child]["fail_ratio"]
        values["down_max_overload"] = down_overload
        values["down_max_latency_ratio"] = down_latency
        values["log_down_max_latency_ratio"] = log_ratio(down_latency)
        values["down_max_fail_ratio"] = down_fail

        parent = PARENT_EDGE[edge]
        if parent is None:
            values["up_rho"] = 0.0
            values["up_overload_ratio"] = 0.0
        else:
            values["up_rho"] = own_now[parent]["rho"]
            values["up_overload_ratio"] = own_now[parent]["overload_ratio"]

        values["edge_gateway_order"] = 1.0 if edge == EDGES[0] else 0.0
        values["edge_order_payment"] = 1.0 if edge == EDGES[1] else 0.0
        values["edge_order_inventory"] = 1.0 if edge == EDGES[2] else 0.0

        result[edge] = values
    return result


def feature_vector(values, feature_names=FEATURE_NAMES):
    vector = []
    for name in feature_names:
        vector.append(float(values[name]))
    return vector


def risk_probability(model, values):
    z = model["intercept"]
    i = 0
    for name in model["feature_names"]:
        std = model["std"][i]
        if std <= 0:
            std = 1.0
        z = z + model["weights"][i] * (float(values[name]) - model["mean"][i]) / std
        i = i + 1
    if z > 35:
        return 1.0
    if z < -35:
        return 0.0
    return 1.0 / (1.0 + math.exp(-z))
