import argparse
import random
import sys
import threading
import time

import requests

from common.config import GATEWAY_URL, USER_TIMEOUT_SEC, DEFAULT_SERVICE_CONFIGS, make_dirs
from common.fault_injection import write_config, reset_all_configs
from common.telemetry import log_user_request, log_scenario, wait_seconds_until, read_scenarios


LEGACY_MODES = {
    "failure_storm": ("spike", "error"),
    "latency_spike": ("spike", "latency"),
    "gradual_degradation": ("gradual", "latency"),
}

current_phase = {"name": "normal"}
outstanding = []
outstanding_lock = threading.Lock()


def fault_config(target, fault, severity, peak_latency_ms, peak_failure_rate):
    normal = DEFAULT_SERVICE_CONFIGS[target]
    config = {}
    if fault == "latency":
        peak_min = peak_latency_ms - 100
        peak_max = peak_latency_ms + 100
        config["min_latency_ms"] = int(normal["min_latency_ms"] + (peak_min - normal["min_latency_ms"]) * severity)
        config["max_latency_ms"] = int(normal["max_latency_ms"] + (peak_max - normal["max_latency_ms"]) * severity)
    else:
        config["failure_rate"] = normal["failure_rate"] + (peak_failure_rate - normal["failure_rate"]) * severity
    return config


def apply_system_settings(queue_wait_sec):
    reset_all_configs()
    write_config("order_service", {"queue_wait_sec": queue_wait_sec})


def apply_fault(target, fault, severity, args):
    config = fault_config(target, fault, severity, args.peak_latency_ms, args.peak_failure_rate)
    if target == "order_service":
        config["queue_wait_sec"] = args.queue_wait
    write_config(target, config)
    return config


def fire_one_request(scenario_id, run_type):
    start_time = time.time()
    succeeded = False
    status_code = 0

    params = {"scenario_id": scenario_id, "run_type": run_type}
    headers = {"X-Deadline": "%.6f" % (start_time + USER_TIMEOUT_SEC)}
    try:
        response = requests.get(GATEWAY_URL, params=params, headers=headers,
                                timeout=USER_TIMEOUT_SEC)
        status_code = response.status_code
        succeeded = (status_code == 200)
    except requests.exceptions.RequestException:
        succeeded = False

    log_user_request(scenario_id, run_type, start_time, time.time(), succeeded, status_code)


def traffic_loop(scenario_id, rps, arrivals, seed, end_time, stop_flag):
    rng = random.Random(seed)
    next_time = time.time()
    while next_time < end_time and not stop_flag["stop"]:
        wait_seconds_until(next_time)
        if stop_flag["stop"]:
            break

        t = threading.Thread(target=fire_one_request,
                             args=(scenario_id, current_phase["name"]))
        t.daemon = True
        t.start()
        with outstanding_lock:
            outstanding.append(t)

        if arrivals == "poisson":
            next_time = next_time + rng.expovariate(rps)
        else:
            next_time = next_time + 1.0 / rps


def build_schedule(args):
    steps = [("normal", args.normal_sec, 0.0)]

    if args.mode == "none":
        steps.append(("normal", args.hold_sec + args.recovery_sec, 0.0))
        return steps

    if args.mode == "gradual":
        step_sec = float(args.ramp_sec) / args.ramp_steps
        for i in range(args.ramp_steps):
            severity = float(i + 1) / args.ramp_steps
            steps.append(("ramp", step_sec, severity))
        if args.hold_sec > 0:
            steps.append(("fault", args.hold_sec, 1.0))
    else:
        steps.append(("fault", args.hold_sec, 1.0))

    steps.append(("recovery", args.recovery_sec, 0.0))
    return steps


def merge_phases(step_log):
    phases = []
    for name, start, end in step_log:
        if len(phases) > 0 and phases[-1]["name"] == name:
            phases[-1]["end"] = end
        else:
            phases.append({"name": name, "start": start, "end": end})
    return phases


def check_args(args):
    problems = []
    if args.rps <= 0:
        problems.append("--rps must be above 0")
    if args.normal_sec < 4:
        problems.append("--normal_sec must be at least 4 (the healthy baseline is measured there)")
    if args.mode == "gradual" and args.ramp_steps < 1:
        problems.append("--ramp_steps must be at least 1")
    if args.mode == "gradual" and args.ramp_sec <= 0:
        problems.append("--ramp_sec must be above 0")
    if args.mode == "spike" and args.hold_sec <= 0:
        problems.append("--hold_sec must be above 0 for a spike")
    if args.recovery_sec < 0:
        problems.append("--recovery_sec cannot be negative")
    if not (0.0 <= args.peak_failure_rate <= 1.0):
        problems.append("--peak_failure_rate must be between 0 and 1")
    if args.peak_latency_ms < 100:
        problems.append("--peak_latency_ms must be at least 100")
    if args.queue_wait <= 0:
        problems.append("--queue_wait must be above 0")
    return problems


def parse_args(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--scenario_id", type=str, required=True)
    parser.add_argument("--group", type=str, default="manual")
    parser.add_argument("--mode", type=str, default="gradual",
                        choices=["spike", "gradual", "none", "failure_storm", "latency_spike",
                                 "gradual_degradation"])
    parser.add_argument("--fault", type=str, default="latency", choices=["latency", "error"])
    parser.add_argument("--target", type=str, default="payment_service",
                        choices=["payment_service", "inventory_service", "order_service"])
    parser.add_argument("--normal_sec", type=float, default=10)
    parser.add_argument("--ramp_sec", type=float, default=10)
    parser.add_argument("--ramp_steps", type=int, default=5)
    parser.add_argument("--hold_sec", type=float, default=10)
    parser.add_argument("--recovery_sec", type=float, default=20)
    parser.add_argument("--rps", type=float, default=20)
    parser.add_argument("--arrivals", type=str, default="poisson", choices=["poisson", "uniform"])
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument("--peak_latency_ms", type=float, default=800)
    parser.add_argument("--peak_failure_rate", type=float, default=0.8)
    parser.add_argument("--queue_wait", type=float, default=1.0,
                        help="how long order_service lets a request wait for a slot (sec)")
    parser.add_argument("--quiet", action="store_true")
    parser.add_argument("--spike_sec", type=float, default=None)
    parser.add_argument("--storm_sec", type=float, default=None)
    parser.add_argument("--after_sec", type=float, default=None)
    args = parser.parse_args(argv)

    if args.mode in LEGACY_MODES:
        old_mode = args.mode
        args.mode, args.fault = LEGACY_MODES[old_mode]
        if old_mode == "gradual_degradation" and args.spike_sec is not None:
            args.ramp_sec = args.spike_sec
        elif args.spike_sec is not None:
            args.hold_sec = args.spike_sec
        if args.storm_sec is not None:
            args.hold_sec = args.storm_sec
    if args.after_sec is not None:
        args.recovery_sec = args.after_sec

    return args


def run_scenario(args):
    make_dirs()
    say = print
    if args.quiet:
        say = no_print

    schedule = build_schedule(args)
    total_sec = 0.0
    for name, duration, severity in schedule:
        total_sec = total_sec + duration

    say("scenario %s: %s %s fault on %s, %.0f users/sec (%s), order queue wait %.1fs, %.0f sec total"
        % (args.scenario_id, args.mode, args.fault, args.target, args.rps,
           args.arrivals, args.queue_wait, total_sec))

    apply_system_settings(args.queue_wait)

    stop_flag = {"stop": False}
    current_phase["name"] = "normal"
    with outstanding_lock:
        del outstanding[:]

    start_time = time.time()
    end_time = start_time + total_sec
    loader = threading.Thread(target=traffic_loop,
                              args=(args.scenario_id, args.rps, args.arrivals,
                                    args.seed, end_time, stop_flag))
    loader.daemon = True

    step_log = []
    interrupted = False
    step_start = start_time
    try:
        loader.start()
        for name, duration, severity in schedule:
            current_phase["name"] = name
            if name == "normal" or name == "recovery":
                apply_system_settings(args.queue_wait)
                say("  [%5.1fs] %s: fault removed / not active" % (step_start - start_time, name))
            else:
                config = apply_fault(args.target, args.fault, severity, args)
                say("  [%5.1fs] %s: %s -> %s" % (step_start - start_time, name, args.target, config))

            step_end = step_start + duration
            wait_seconds_until(step_end)
            step_log.append((name, step_start, step_end))
            step_start = step_end

        loader.join()
        say("  traffic finished, waiting for the last requests to come back...")
        wait_for_outstanding(USER_TIMEOUT_SEC + 2)
    except KeyboardInterrupt:
        interrupted = True
        say("\ninterrupted -- removing the fault before exiting")
        stop_flag["stop"] = True
        step_log.append((current_phase["name"], step_start, time.time()))
    finally:
        apply_system_settings(1.0)

    phases = merge_phases(step_log)
    with outstanding_lock:
        n_requests = len(outstanding)

    manifest = {
        "scenario_id": args.scenario_id,
        "group": args.group,
        "mode": args.mode,
        "fault": args.fault,
        "target": args.target,
        "rps": args.rps,
        "arrivals": args.arrivals,
        "seed": args.seed,
        "queue_wait": args.queue_wait,
        "peak_latency_ms": args.peak_latency_ms,
        "peak_failure_rate": args.peak_failure_rate,
        "normal_sec": args.normal_sec,
        "ramp_sec": args.ramp_sec if args.mode == "gradual" else 0,
        "hold_sec": args.hold_sec,
        "recovery_sec": args.recovery_sec,
        "start": start_time,
        "end": phases[-1]["end"] if len(phases) > 0 else time.time(),
        "phases": phases,
        "requests_sent": n_requests,
        "interrupted": interrupted,
    }
    log_scenario(manifest)

    if interrupted:
        raise KeyboardInterrupt

    say("done: %d user requests sent. Logs are in the data/ folder." % n_requests)
    return manifest


def id_already_used(scenario_id):
    for manifest in read_scenarios():
        if manifest["scenario_id"] == scenario_id and not manifest.get("interrupted"):
            return True
    return False


def wait_for_outstanding(max_wait_sec):
    deadline = time.time() + max_wait_sec
    with outstanding_lock:
        threads = list(outstanding)
    for t in threads:
        remaining = deadline - time.time()
        if remaining <= 0:
            break
        t.join(remaining)


def no_print(*args):
    pass


def main():
    args = parse_args()
    problems = check_args(args)
    if len(problems) > 0:
        for problem in problems:
            print("error: " + problem)
        sys.exit(2)

    try:
        requests.get(GATEWAY_URL.replace("/checkout", "/health"), timeout=2)
    except requests.exceptions.RequestException:
        print("error: the gateway is not answering. Start the services first:  python run_all.py")
        sys.exit(1)

    from run_all import wait_until_idle
    if not wait_until_idle(timeout_sec=2):
        print("waiting for the previous run's leftover requests to finish...")
        if not wait_until_idle(timeout_sec=60):
            print("warning: the services are still busy after 60s; starting anyway")

    if id_already_used(args.scenario_id):
        print("error: scenario id '%s' was already used. Pick a new id (e.g. %s_2)," % (args.scenario_id, args.scenario_id))
        print("so the two runs are never mixed up.")
        sys.exit(2)

    try:
        run_scenario(args)
    except KeyboardInterrupt:
        print("stopped. The fault was removed and this run is marked as interrupted")
        print("(analysis scripts ignore interrupted runs).")
        sys.exit(130)


if __name__ == "__main__":
    main()
