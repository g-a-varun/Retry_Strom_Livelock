import glob
import json
import os
import threading
import time

from common.config import DATA_DIR
from common.safe_files import remove_file


USER_LOG_NAME = "user_requests.jsonl"
SCENARIO_LOG_NAME = "scenarios.jsonl"
CONTROLLER_LOG_NAME = "controller_log.jsonl"

write_lock = threading.Lock()


def data_path(name):
    return os.path.join(DATA_DIR, name)


def call_log_path(source_service):
    return data_path("telemetry_%s.jsonl" % source_service)


def server_log_path(service_name):
    return data_path("server_%s.jsonl" % service_name)


def append_record(path, record):
    line = json.dumps(record)
    with write_lock:
        folder = os.path.dirname(path)
        if not os.path.exists(folder):
            os.makedirs(folder, exist_ok=True)
        with open(path, "a", encoding="utf-8") as f:
            f.write(line + "\n")


def log_call(source_service, dest_service, attempt_number, start_time, end_time,
             backoff_delay_ms, response_code, retry_policy, budget_blocked,
             call_id, scenario_id, run_type):
    record = {
        "timestamp": end_time,
        "start_time": start_time,
        "source_service": source_service,
        "dest_service": dest_service,
        "attempt_number": attempt_number,
        "backoff_delay_ms": backoff_delay_ms,
        "response_code": response_code,
        "latency_ms": (end_time - start_time) * 1000.0,
        "retry_policy": retry_policy,
        "budget_blocked": budget_blocked,
        "call_id": call_id,
        "scenario_id": scenario_id,
        "run_type": run_type,
    }
    append_record(call_log_path(source_service), record)


def log_server_request(service_name, arrived, slot_time, done_time, status,
                       deadline, scenario_id, run_type):
    orphaned = False
    expired_in_queue = False
    if deadline is not None:
        orphaned = done_time > deadline
        expired_in_queue = slot_time > deadline

    record = {
        "timestamp": done_time,
        "service": service_name,
        "arrived": arrived,
        "slot_time": slot_time,
        "status": status,
        "deadline": deadline,
        "orphaned": orphaned,
        "expired_in_queue": expired_in_queue,
        "queue_wait_ms": (slot_time - arrived) * 1000.0,
        "service_ms": (done_time - slot_time) * 1000.0,
        "scenario_id": scenario_id,
        "run_type": run_type,
    }
    append_record(server_log_path(service_name), record)


def log_user_request(scenario_id, run_type, start_time, end_time, succeeded, status_code):
    record = {
        "timestamp": end_time,
        "start_time": start_time,
        "scenario_id": scenario_id,
        "run_type": run_type,
        "succeeded": succeeded,
        "status_code": status_code,
        "total_time_ms": (end_time - start_time) * 1000.0,
    }
    append_record(data_path(USER_LOG_NAME), record)


def log_scenario(manifest):
    append_record(data_path(SCENARIO_LOG_NAME), manifest)


def log_controller_decision(record):
    append_record(data_path(CONTROLLER_LOG_NAME), record)


SCENARIO_KEY = '"scenario_id": "'


def scenario_of_line(line):
    start = line.find(SCENARIO_KEY)
    if start < 0:
        return None
    start = start + len(SCENARIO_KEY)
    return line[start:line.find('"', start)]


def read_jsonl(path, wanted=None):
    records = []
    skipped = 0

    if not os.path.exists(path):
        return records, skipped

    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line == "":
                continue
            if wanted is not None and scenario_of_line(line) not in wanted:
                continue
            try:
                records.append(json.loads(line))
            except ValueError:
                skipped = skipped + 1

    return records, skipped


def record_timestamp(record):
    return record["timestamp"]


def keep_scenarios(records, scenario_ids):
    if scenario_ids is None:
        return records
    wanted = set(scenario_ids)
    kept = []
    for record in records:
        if record.get("scenario_id") in wanted:
            kept.append(record)
    return kept


def read_many(pattern, scenario_ids=None):
    records = []
    skipped = 0
    wanted = None
    if scenario_ids is not None:
        wanted = set(scenario_ids)
    for path in sorted(glob.glob(data_path(pattern))):
        part, part_skipped = read_jsonl(path, wanted)
        records.extend(part)
        skipped = skipped + part_skipped

    records = keep_scenarios(records, scenario_ids)
    records.sort(key=record_timestamp)
    return records, skipped


def within_run(records, manifest, time_key="timestamp"):
    first = manifest["start"] - 2.0
    last = manifest["end"] + 90.0
    kept = []
    for record in records:
        t = record.get(time_key, record["timestamp"])
        if first <= t <= last:
            kept.append(record)
    return kept


def read_call_records(scenario_ids=None):
    return read_many("telemetry_*.jsonl", scenario_ids)


def read_server_records(scenario_ids=None):
    return read_many("server_*.jsonl", scenario_ids)


def read_user_records(scenario_ids=None):
    return read_many(USER_LOG_NAME, scenario_ids)


def read_scenarios():
    records, skipped = read_jsonl(data_path(SCENARIO_LOG_NAME))
    return records


def find_scenario(scenario_id):
    found = None
    for manifest in read_scenarios():
        if manifest["scenario_id"] == scenario_id:
            found = manifest
    return found


def scenarios_in_group(group):
    manifests = []
    for manifest in read_scenarios():
        if manifest.get("group") == group:
            manifests.append(manifest)
    return manifests


def reset_logs():
    patterns = ["telemetry_*.jsonl", "server_*.jsonl", USER_LOG_NAME,
                SCENARIO_LOG_NAME, CONTROLLER_LOG_NAME]
    for pattern in patterns:
        for path in glob.glob(data_path(pattern)):
            remove_file(path)


def make_tail_state(start_at_end):
    state = {"offsets": {}, "partial": {}}
    if start_at_end:
        for path in glob.glob(data_path("telemetry_*.jsonl")):
            state["offsets"][path] = os.path.getsize(path)
            state["partial"][path] = b""
    return state


def read_new_call_records(state):
    new_records = []

    for path in sorted(glob.glob(data_path("telemetry_*.jsonl"))):
        offset = state["offsets"].get(path, 0)
        partial = state["partial"].get(path, b"")

        try:
            size = os.path.getsize(path)
        except OSError:
            continue

        if size < offset:
            offset = 0
            partial = b""

        if size == offset:
            continue

        try:
            with open(path, "rb") as f:
                f.seek(offset)
                chunk = f.read()
                offset = f.tell()
        except OSError:
            continue

        data = partial + chunk
        lines = data.split(b"\n")
        partial = lines[-1]
        for line in lines[:-1]:
            line = line.strip()
            if len(line) == 0:
                continue
            try:
                new_records.append(json.loads(line.decode("utf-8")))
            except ValueError:
                pass

        state["offsets"][path] = offset
        state["partial"][path] = partial

    return new_records


def wait_seconds_until(target_time):
    delay = target_time - time.time()
    if delay > 0:
        time.sleep(delay)
