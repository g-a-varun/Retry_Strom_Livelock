import json
import os
import time


REPLACE_RETRIES = 50
REPLACE_RETRY_SLEEP_SEC = 0.01


def write_json_atomic(path, data):
    tmp_path = "%s.%d.tmp" % (path, os.getpid())
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(data, f)

    for attempt in range(REPLACE_RETRIES):
        try:
            os.replace(tmp_path, path)
            return True
        except PermissionError:
            time.sleep(REPLACE_RETRY_SLEEP_SEC)

    try:
        os.remove(tmp_path)
    except OSError:
        pass
    return False


def read_json(path):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def remove_file(path):
    for attempt in range(REPLACE_RETRIES):
        try:
            if os.path.exists(path):
                os.remove(path)
            return True
        except PermissionError:
            time.sleep(REPLACE_RETRY_SLEEP_SEC)
        except OSError:
            return False
    return False
