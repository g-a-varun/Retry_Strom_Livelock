import json
import os
import time

# Several processes share small JSON files: the scenario runner writes the
# fault config while four services read it, and the controller writes the
# retry policy while the services read it. Two things can go wrong:
#
# 1. a reader opens the file while the writer is half way through, and gets
#    broken JSON. Fixed by writing to a temp file and swapping it in with
#    os.replace, which is atomic.
# 2. on Windows, os.replace fails with PermissionError if another process
#    has the file open at that exact moment. Fixed by retrying a few times.

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

    # could not swap the file in. leave the old file alone rather than
    # crash the caller, and clean up the temp file
    try:
        os.remove(tmp_path)
    except OSError:
        pass
    return False


def read_json(path):
    # returns None if the file is missing, locked, or not valid JSON.
    # the caller decides what to fall back to.
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
