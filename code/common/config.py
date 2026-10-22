import os

CODE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFIG_DIR = os.path.join(CODE_DIR, "configs")
DATA_DIR = os.path.join(CODE_DIR, "data")
RESULTS_DIR = os.path.join(CODE_DIR, "results")
FIGURES_DIR = os.path.join(RESULTS_DIR, "figures")

GATEWAY_URL = "http://127.0.0.1:5000/checkout"
ORDER_URL = "http://127.0.0.1:5003/checkout"
PAYMENT_URL = "http://127.0.0.1:5001/pay"
INVENTORY_URL = "http://127.0.0.1:5002/reserve"

SERVICE_PORTS = {
    "gateway": 5000,
    "payment_service": 5001,
    "inventory_service": 5002,
    "order_service": 5003,
}

EDGES = [
    ("gateway", "order_service"),
    ("order_service", "payment_service"),
    ("order_service", "inventory_service"),
]

NODES = ["gateway", "order_service", "payment_service", "inventory_service"]

PARENT_EDGE = {
    ("gateway", "order_service"): None,
    ("order_service", "payment_service"): ("gateway", "order_service"),
    ("order_service", "inventory_service"): ("gateway", "order_service"),
}

CHILD_EDGES = {
    ("gateway", "order_service"): [("order_service", "payment_service"),
                                   ("order_service", "inventory_service")],
    ("order_service", "payment_service"): [],
    ("order_service", "inventory_service"): [],
}

DEFAULT_SERVICE_CONFIGS = {
    "order_service": {
        "failure_rate": 0.02,
        "min_latency_ms": 10,
        "max_latency_ms": 40,
        "queue_wait_sec": 1.0,
    },
    "payment_service": {
        "failure_rate": 0.05,
        "min_latency_ms": 30,
        "max_latency_ms": 90,
        "queue_wait_sec": 1.0,
    },
    "inventory_service": {
        "failure_rate": 0.05,
        "min_latency_ms": 30,
        "max_latency_ms": 90,
        "queue_wait_sec": 1.0,
    },
}

DEFAULT_SLOTS = {
    "order_service": 8,
    "payment_service": 2,
    "inventory_service": 2,
}

DEFAULT_MAX_ATTEMPTS = 3
DEFAULT_BACKOFF_BASE_MS = 100
DEFAULT_TIMEOUT_MS = 2000

USER_TIMEOUT_SEC = 15.0


def edge_name(edge):
    return edge[0] + "->" + edge[1]


def make_dirs():
    for path in [DATA_DIR, RESULTS_DIR, FIGURES_DIR]:
        if not os.path.exists(path):
            os.makedirs(path)
