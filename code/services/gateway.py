import os
import sys
import argparse
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from flask import Flask, request, jsonify
from common.config import SERVICE_PORTS, ORDER_URL
from common.retry_client import call_with_retry
from common.service_helpers import (quiet_flask_logs, change_counter,
                                    get_counters, read_deadline)
from common.telemetry import log_server_request

app = Flask(__name__)
quiet_flask_logs()

SERVICE_NAME = "gateway"


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
        order_ok = call_with_retry(SERVICE_NAME, "order_service", ORDER_URL,
                                   scenario_id, run_type)
        status = 200 if order_ok else 502

        log_server_request(SERVICE_NAME, arrived, arrived, time.time(), status,
                           deadline, scenario_id, run_type)
        change_counter("served", 1)

        if order_ok:
            return "checkout complete", 200
        return "checkout failed", 502
    finally:
        change_counter("inflight", -1)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=SERVICE_PORTS[SERVICE_NAME])
    args = parser.parse_args()

    print("gateway starting (no slot limit)")
    app.run(host="127.0.0.1", port=args.port, threaded=True)
