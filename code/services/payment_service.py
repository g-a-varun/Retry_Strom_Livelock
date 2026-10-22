import os
import sys
import argparse
import threading
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from flask import Flask, request, jsonify
from common.config import DEFAULT_SLOTS, SERVICE_PORTS
from common.fault_injection import load_config, do_work
from common.service_helpers import (quiet_flask_logs, change_counter,
                                    get_counters, read_deadline)
from common.telemetry import log_server_request

app = Flask(__name__)
quiet_flask_logs()

SERVICE_NAME = "payment_service"

worker_slots = None


@app.route("/health")
def health():
    return "ok", 200


@app.route("/stats")
def stats():
    return jsonify(get_counters())


@app.route("/pay")
def pay():
    arrived = time.time()
    scenario_id = request.args.get("scenario_id", "unknown")
    run_type = request.args.get("run_type", "unknown")
    deadline = read_deadline(request)

    change_counter("inflight", 1)
    try:
        config = load_config(SERVICE_NAME)

        change_counter("waiting", 1)
        got_slot = worker_slots.acquire(timeout=config["queue_wait_sec"])
        change_counter("waiting", -1)
        slot_time = time.time()

        if not got_slot:
            log_server_request(SERVICE_NAME, arrived, slot_time, slot_time, 503,
                               deadline, scenario_id, run_type)
            return "payment service busy", 503

        try:
            config = load_config(SERVICE_NAME)
            should_fail, latency_ms = do_work(config)
        finally:
            worker_slots.release()

        status = 500 if should_fail else 200
        log_server_request(SERVICE_NAME, arrived, slot_time, time.time(), status,
                           deadline, scenario_id, run_type)
        change_counter("served", 1)

        if should_fail:
            return "payment failed", 500
        return "payment ok", 200
    finally:
        change_counter("inflight", -1)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=SERVICE_PORTS[SERVICE_NAME])
    parser.add_argument("--slots", type=int, default=DEFAULT_SLOTS[SERVICE_NAME])
    args = parser.parse_args()

    if args.slots < 1:
        print("slots must be at least 1")
        sys.exit(1)

    worker_slots = threading.Semaphore(args.slots)
    print("payment_service starting with %d worker slots" % args.slots)

    app.run(host="127.0.0.1", port=args.port, threaded=True)
