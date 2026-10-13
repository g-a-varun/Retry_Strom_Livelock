import random
import threading
import time
import uuid

import requests

from common.telemetry import log_call
from common.retry_policy import get_retry_policy, describe_policy

# ---- retry budget (the "SRE retry budget" baseline) ----
#
# A token bucket per edge. Every fresh call puts budget_ratio tokens in,
# every retry takes one token out. So over time retries can be at most
# budget_ratio of fresh calls (0.1 = 10%), with a small burst allowance.
# This is the approach from the Google SRE book / Finagle / gRPC retry
# throttling. It is only used when the policy has a budget_ratio set.

BUDGET_MAX_TOKENS = 10.0

budget_lock = threading.Lock()
budget_tokens = {}


def budget_deposit(name, ratio):
    with budget_lock:
        tokens = budget_tokens.get(name, BUDGET_MAX_TOKENS) + ratio
        if tokens > BUDGET_MAX_TOKENS:
            tokens = BUDGET_MAX_TOKENS
        budget_tokens[name] = tokens


def budget_take(name):
    with budget_lock:
        tokens = budget_tokens.get(name, BUDGET_MAX_TOKENS)
        # small tolerance: 10 deposits of 0.1 add up to 0.9999999999999999
        # in floating point, which must still buy one retry
        if tokens >= 1.0 - 1e-9:
            budget_tokens[name] = tokens - 1.0
            return True
        return False


def backoff_with_jitter_ms(backoff_base_ms, attempt_number):
    # exponential backoff: 100, 200, 400 ... ms.
    # "equal jitter": half of it is fixed, the other half random. Without
    # jitter, clients that failed together retry together, in lock-step.
    full_delay = backoff_base_ms * (2 ** (attempt_number - 1))
    return full_delay / 2.0 + random.uniform(0, full_delay / 2.0)


def call_with_retry(source_service, dest_service, url, scenario_id, run_type):
    name = source_service + "->" + dest_service
    policy = get_retry_policy(name)

    max_attempts = policy["max_attempts"]
    timeout_sec = policy["timeout_ms"] / 1000.0
    budget_ratio = policy["budget_ratio"]
    policy_text = describe_policy(policy)

    params = {"scenario_id": scenario_id, "run_type": run_type}
    # one id shared by all attempts of this call, so the logs show which
    # retries belong together
    call_id = uuid.uuid4().hex[:12]

    if budget_ratio is not None:
        budget_deposit(name, budget_ratio)

    attempt_number = 1
    while True:
        start_time = time.time()
        # tell the callee when we will stop waiting, so it can log whether
        # it finished in time or worked for nothing (an "orphaned" request)
        deadline = start_time + timeout_sec
        headers = {"X-Deadline": "%.6f" % deadline}

        try:
            response = requests.get(url, params=params, headers=headers, timeout=timeout_sec)
            response_code = response.status_code
        except requests.exceptions.RequestException:
            # timeout or connection refused: recorded as code 0
            response_code = 0

        end_time = time.time()
        success = (response_code == 200)

        will_retry = False
        budget_blocked = False
        if not success and attempt_number < max_attempts:
            if budget_ratio is None:
                will_retry = True
            elif budget_take(name):
                will_retry = True
            else:
                budget_blocked = True

        backoff_delay_ms = 0
        if will_retry:
            backoff_delay_ms = backoff_with_jitter_ms(policy["backoff_base_ms"], attempt_number)

        log_call(
            source_service=source_service,
            dest_service=dest_service,
            attempt_number=attempt_number,
            start_time=start_time,
            end_time=end_time,
            backoff_delay_ms=backoff_delay_ms,
            response_code=response_code,
            retry_policy=policy_text,
            budget_blocked=budget_blocked,
            call_id=call_id,
            scenario_id=scenario_id,
            run_type=run_type,
        )

        if success:
            return True
        if not will_retry:
            return False

        time.sleep(backoff_delay_ms / 1000.0)
        attempt_number = attempt_number + 1
