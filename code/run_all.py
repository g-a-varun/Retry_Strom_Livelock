import argparse
import os
import socket
import subprocess
import sys
import time

import requests

from common.config import CODE_DIR, SERVICE_PORTS, DEFAULT_SLOTS, make_dirs
from common.fault_injection import reset_all_configs
from common.retry_policy import reset_policy

# start order matters a little: leaf services first, gateway last
SERVICES = [
    ("payment_service", os.path.join(CODE_DIR, "services", "payment_service.py")),
    ("inventory_service", os.path.join(CODE_DIR, "services", "inventory_service.py")),
    ("order_service", os.path.join(CODE_DIR, "services", "order_service.py")),
    ("gateway", os.path.join(CODE_DIR, "services", "gateway.py")),
]


def port_in_use(port):
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(0.5)
    try:
        result = sock.connect_ex(("127.0.0.1", port))
    finally:
        sock.close()
    return result == 0


def wait_until_up(port, timeout_sec=15):
    deadline = time.time() + timeout_sec
    while time.time() < deadline:
        try:
            response = requests.get("http://127.0.0.1:%d/health" % port, timeout=0.5)
            if response.status_code == 200:
                return True
        except requests.exceptions.RequestException:
            pass
        time.sleep(0.2)
    return False


def stop_services(processes):
    for name, proc in processes:
        if proc.poll() is None:
            proc.terminate()
    for name, proc in processes:
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()


def start_services(slots_by_service, log_file=None, verbose=True):
    make_dirs()

    busy_ports = []
    for name, script_path in SERVICES:
        if port_in_use(SERVICE_PORTS[name]):
            busy_ports.append("%s (port %d)" % (name, SERVICE_PORTS[name]))
    if len(busy_ports) > 0:
        print("ERROR: these ports are already in use: %s" % ", ".join(busy_ports))
        print("An old copy of the services is probably still running.")
        print("Close it (Ctrl+C in its terminal, or end python.exe in Task Manager)")
        print("and try again.")
        return None

    # always start from a clean, healthy state: no leftover fault from a
    # run that was stopped half way, no leftover retry policy
    reset_all_configs()
    reset_policy()

    processes = []
    for name, script_path in SERVICES:
        command = [sys.executable, script_path, "--port", str(SERVICE_PORTS[name])]
        if name in slots_by_service:
            command.append("--slots")
            command.append(str(slots_by_service[name]))

        if verbose:
            print("starting %s on port %d" % (name, SERVICE_PORTS[name]))
        if log_file is None:
            proc = subprocess.Popen(command, cwd=CODE_DIR)
        else:
            proc = subprocess.Popen(command, cwd=CODE_DIR, stdout=log_file,
                                    stderr=subprocess.STDOUT)
        processes.append((name, proc))

    for name, proc in processes:
        if not wait_until_up(SERVICE_PORTS[name]):
            print("ERROR: %s did not start. Stopping everything." % name)
            stop_services(processes)
            return None
        if verbose:
            print("%s is up" % name)

    return processes


def service_counters(name):
    try:
        response = requests.get("http://127.0.0.1:%d/stats" % SERVICE_PORTS[name], timeout=1.0)
        return response.json()
    except (requests.exceptions.RequestException, ValueError):
        return None


def wait_until_idle(timeout_sec=40, verbose=False):
    # Wait until no service is working on anything. Needed between runs:
    # with a long queue, abandoned requests can keep a service busy for
    # several seconds after the users have gone, and that leftover work
    # must not leak into the next run.
    deadline = time.time() + timeout_sec
    quiet_since = None
    while time.time() < deadline:
        total = 0
        reachable = True
        for name, script_path in SERVICES:
            counters = service_counters(name)
            if counters is None:
                reachable = False
                break
            total = total + counters["inflight"]

        if not reachable:
            return False

        if total == 0:
            if quiet_since is None:
                quiet_since = time.time()
            if time.time() - quiet_since >= 1.0:
                return True
        else:
            quiet_since = None
            if verbose:
                print("  waiting for %d request(s) still in flight..." % total)
        time.sleep(0.5)
    return False


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--payment_slots", type=int, default=DEFAULT_SLOTS["payment_service"])
    parser.add_argument("--inventory_slots", type=int, default=DEFAULT_SLOTS["inventory_service"])
    parser.add_argument("--order_slots", type=int, default=DEFAULT_SLOTS["order_service"])
    args = parser.parse_args()

    slots_by_service = {
        "payment_service": args.payment_slots,
        "inventory_service": args.inventory_slots,
        "order_service": args.order_slots,
    }

    processes = start_services(slots_by_service)
    if processes is None:
        sys.exit(1)

    print("all services started. press Ctrl+C to stop.")

    try:
        while True:
            time.sleep(1)
            for name, proc in processes:
                if proc.poll() is not None:
                    print("ERROR: %s stopped unexpectedly (exit code %s). Stopping everything."
                          % (name, proc.returncode))
                    stop_services(processes)
                    sys.exit(1)
    except KeyboardInterrupt:
        print("stopping all services...")
        stop_services(processes)


if __name__ == "__main__":
    main()
