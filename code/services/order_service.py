import os
import sys
import argparse
import threading
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from flask import Flask, request, jsonify
from common.config import DEFAULT_SLOTS, SERVICE_PORTS, PAYMENT_URL, INVENTORY_URL
from common.fault_injection import load_config, do_work
from common.retry_client import call_with_retry
from common.service_helpers import (quiet_flask_logs, change_counter,
                                    get_counters, read_deadline)
from common.telemetry import log_server_request

app = Flask(__name__)
quiet_flask_logs()

SERVICE_NAME = "order_service"

worker_slots = None


@app.route("/health")
def health():
    return "ok", 200


@app.route("/stats")
def stats():
    return jsonify(get_counters())


@app.route("/checkout")
def checkout():
    arrived = time.time()
    scenario_id = request.args.get("scenario_id", "unknown")
    run_type = request.args.get("run_type", "unknown")
    deadline = read_deadline(request)

    change_counter("inflight", 1)
    try:
        config = load_config(SERVICE_NAME)

        # queue_wait_sec is how long a request may wait for a free slot.
        # Short (1s): requests that cannot be served are rejected quickly.
        # Long (10s): like a big thread-pool queue. A request can still be
        # waiting long after the gateway that sent it has given up.
        change_counter("waiting", 1)
        got_slot = worker_slots.acquire(timeout=config["queue_wait_sec"])
        change_counter("waiting", -1)
        slot_time = time.time()

        if not got_slot:
            log_server_request(SERVICE_NAME, arrived, slot_time, slot_time, 503,
                               deadline, scenario_id, run_type)
            return "order service busy", 503

        status = 200
        try:
            config = load_config(SERVICE_NAME)
            should_fail, latency_ms = do_work(config)

            if should_fail:
                status = 500
            else:
                # note: this service does NOT check whether the gateway is
                # still waiting. Like most real services, it finishes the
                # work anyway. That is where wasted (orphaned) work comes from.
                payment_ok = call_with_retry(SERVICE_NAME, "payment_service",
                                             PAYMENT_URL, scenario_id, run_type)
                inventory_ok = call_with_retry(SERVICE_NAME, "inventory_service",
                                               INVENTORY_URL, scenario_id, run_type)
                if not (payment_ok and inventory_ok):
                    status = 502
        finally:
            worker_slots.release()

        log_server_request(SERVICE_NAME, arrived, slot_time, time.time(), status,
                           deadline, scenario_id, run_type)
        change_counter("served", 1)

        if status == 200:
            return "order complete", 200
        if status == 500:
            return "order failed", 500
        return "order failed downstream", 502
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
    print("order_service starting with %d worker slots" % args.slots)

    app.run(host="127.0.0.1", port=args.port, threaded=True)
