import argparse
import random
import sys
import time

import requests

from common.config import GATEWAY_URL, make_dirs
from common.retry_policy import reset_policy
from common.telemetry import scenarios_in_group
from run_all import wait_until_idle
import scenario_runner


GROUP = "dataset"


def draw_scenario(rng, index):
    params = {}
    params["scenario_id"] = "ds_%02d" % index
    params["fault"] = "latency" if rng.random() < 0.6 else "error"
    params["target"] = "payment_service" if rng.random() < 0.6 else "inventory_service"
    if rng.random() < 0.5:
        params["queue_wait"] = 1.0
    else:
        params["queue_wait"] = float(rng.choice([4, 6, 8, 10]))
    params["rps"] = float(rng.randint(12, 22))
    params["mode"] = "gradual" if rng.random() < 0.8 else "spike"
    params["normal_sec"] = float(rng.randint(10, 12))
    params["ramp_sec"] = float(rng.randint(6, 14))
    params["ramp_steps"] = rng.randint(3, 6)
    params["hold_sec"] = float(rng.randint(6, 12))
    params["recovery_sec"] = float(rng.randint(15, 20))
    params["peak_latency_ms"] = float(rng.choice([500, 600, 700, 800, 900]))
    params["peak_failure_rate"] = rng.choice([0.6, 0.7, 0.8, 0.9])
    params["seed"] = rng.randint(1, 100000)
    return params


def control_scenarios(rng, start_index):
    controls = []
    for i, rps in enumerate([16.0, 18.0, 26.0, 28.0]):
        params = {
            "scenario_id": "ds_%02d" % (start_index + i),
            "fault": "latency",
            "target": "payment_service",
            "queue_wait": 1.0 if i % 2 == 0 else 8.0,
            "rps": rps,
            "mode": "none",
            "normal_sec": 10.0,
            "ramp_sec": 0.0,
            "ramp_steps": 1,
            "hold_sec": 15.0,
            "recovery_sec": 15.0,
            "peak_latency_ms": 800.0,
            "peak_failure_rate": 0.8,
            "seed": rng.randint(1, 100000),
        }
        controls.append(params)
    return controls


def to_argv(params):
    argv = ["--scenario_id", params["scenario_id"], "--group", GROUP, "--quiet"]
    for key in ["mode", "fault", "target"]:
        argv.append("--" + key)
        argv.append(params[key])
    for key in ["normal_sec", "ramp_sec", "ramp_steps", "hold_sec", "recovery_sec",
                "rps", "seed", "peak_latency_ms", "peak_failure_rate"]:
        argv.append("--" + key)
        argv.append(str(params[key]))
    argv.append("--queue_wait")
    argv.append(str(params["queue_wait"]))
    if params["mode"] == "gradual" and params["ramp_steps"] < 1:
        argv[argv.index("--ramp_steps") + 1] = "1"
    return argv


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--n_scenarios", type=int, default=24,
                        help="number of fault scenarios (4 no-fault control runs are added)")
    parser.add_argument("--seed", type=int, default=7)
    parser.add_argument("--no_controls", action="store_true")
    args = parser.parse_args()
    make_dirs()

    try:
        requests.get(GATEWAY_URL.replace("/checkout", "/health"), timeout=2)
    except requests.exceptions.RequestException:
        print("error: the services are not running. Start them first:  python run_all.py")
        sys.exit(1)

    already = set()
    for manifest in scenarios_in_group(GROUP):
        if not manifest.get("interrupted"):
            already.add(manifest["scenario_id"])

    rng = random.Random(args.seed)
    plan = []
    for i in range(args.n_scenarios):
        plan.append(draw_scenario(rng, i + 1))
    if not args.no_controls:
        plan.extend(control_scenarios(rng, args.n_scenarios + 1))

    total_sec = 0.0
    for params in plan:
        total_sec = total_sec + params["normal_sec"] + params["hold_sec"] + params["recovery_sec"]
        if params["mode"] == "gradual":
            total_sec = total_sec + params["ramp_sec"]
    print("dataset plan: %d runs, about %.0f minutes of traffic (plus idle time between runs)"
          % (len(plan), total_sec / 60.0))

    started = time.time()
    for n, params in enumerate(plan):
        if params["scenario_id"] in already:
            print("[%d/%d] %s already recorded, skipping" % (n + 1, len(plan), params["scenario_id"]))
            continue

        print("[%d/%d] %s: %s %s on %s, %.0f rps, queue %.0fs"
              % (n + 1, len(plan), params["scenario_id"], params["mode"], params["fault"],
                 params["target"], params["rps"], params["queue_wait"]))

        reset_policy()
        scenario_args = scenario_runner.parse_args(to_argv(params))
        problems = scenario_runner.check_args(scenario_args)
        if len(problems) > 0:
            print("   skipped, bad settings: %s" % problems)
            continue
        try:
            scenario_runner.run_scenario(scenario_args)
        except KeyboardInterrupt:
            reset_policy()
            print("\nstopped. Finished runs are kept; run this again to continue where it left off.")
            sys.exit(130)

        if not wait_until_idle(timeout_sec=60):
            print("   warning: the services did not go idle within 60s")

    print("dataset done in %.1f minutes" % ((time.time() - started) / 60.0))


if __name__ == "__main__":
    main()
