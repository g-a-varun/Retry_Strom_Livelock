import logging
import threading


counter_lock = threading.Lock()
counters = {"inflight": 0, "waiting": 0, "served": 0}


def quiet_flask_logs():
    logging.getLogger("werkzeug").setLevel(logging.ERROR)


def change_counter(name, amount):
    with counter_lock:
        counters[name] = counters[name] + amount


def get_counters():
    with counter_lock:
        return dict(counters)


def read_deadline(request):
    text = request.headers.get("X-Deadline")
    if text is None:
        return None
    try:
        return float(text)
    except ValueError:
        return None
