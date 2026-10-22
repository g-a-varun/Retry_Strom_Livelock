import os

from common.config import (DATA_DIR, EDGES, edge_name, DEFAULT_MAX_ATTEMPTS,
                           DEFAULT_BACKOFF_BASE_MS, DEFAULT_TIMEOUT_MS)
from common.safe_files import read_json, write_json_atomic, remove_file


POLICY_PATH = os.path.join(DATA_DIR, "retry_policy.json")

last_good_policy = {}


def default_policy():
    return {
        "max_attempts": DEFAULT_MAX_ATTEMPTS,
        "backoff_base_ms": DEFAULT_BACKOFF_BASE_MS,
        "timeout_ms": DEFAULT_TIMEOUT_MS,
        "budget_ratio": None,
    }


def get_retry_policy(name):
    policy = default_policy()

    if not os.path.exists(POLICY_PATH):
        return policy

    data = read_json(POLICY_PATH)
    if data is None:
        if name in last_good_policy:
            return last_good_policy[name]
        return policy

    if name in data and isinstance(data[name], dict):
        entry = data[name]
        for key in policy:
            if key in entry:
                policy[key] = entry[key]

    policy = clean_policy(policy)
    last_good_policy[name] = policy
    return policy


def clean_policy(policy):
    cleaned = default_policy()
    try:
        cleaned["max_attempts"] = max(1, int(policy["max_attempts"]))
    except (TypeError, ValueError):
        pass
    try:
        cleaned["backoff_base_ms"] = max(0, int(policy["backoff_base_ms"]))
    except (TypeError, ValueError):
        pass
    try:
        timeout_ms = int(policy["timeout_ms"])
        if timeout_ms >= 1:
            cleaned["timeout_ms"] = timeout_ms
    except (TypeError, ValueError):
        pass
    try:
        if policy["budget_ratio"] is not None:
            cleaned["budget_ratio"] = max(0.0, float(policy["budget_ratio"]))
    except (TypeError, ValueError):
        pass
    return cleaned


def read_policy_file():
    data = read_json(POLICY_PATH)
    if data is None:
        return {}
    return data


def write_policy_file(policy):
    if not os.path.exists(DATA_DIR):
        os.makedirs(DATA_DIR, exist_ok=True)
    return write_json_atomic(POLICY_PATH, policy)


def set_retry_policy(name, max_attempts, backoff_base_ms,
                     timeout_ms=DEFAULT_TIMEOUT_MS, budget_ratio=None):
    policy = read_policy_file()
    policy[name] = {
        "max_attempts": max_attempts,
        "backoff_base_ms": backoff_base_ms,
        "timeout_ms": timeout_ms,
        "budget_ratio": budget_ratio,
    }
    return write_policy_file(policy)


def write_uniform_policy(max_attempts, backoff_base_ms=DEFAULT_BACKOFF_BASE_MS,
                         timeout_ms=DEFAULT_TIMEOUT_MS, budget_ratio=None):
    policy = {}
    for edge in EDGES:
        policy[edge_name(edge)] = {
            "max_attempts": max_attempts,
            "backoff_base_ms": backoff_base_ms,
            "timeout_ms": timeout_ms,
            "budget_ratio": budget_ratio,
        }
    return write_policy_file(policy)


def reset_policy():
    remove_file(POLICY_PATH)


def describe_policy(policy):
    text = "base_%dms_max_%d_timeout_%dms" % (
        policy["backoff_base_ms"], policy["max_attempts"], policy["timeout_ms"])
    if policy["budget_ratio"] is not None:
        text = text + "_budget_%.2f" % policy["budget_ratio"]
    return text
