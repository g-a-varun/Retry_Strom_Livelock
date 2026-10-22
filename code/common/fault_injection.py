import os
import random
import time

from common.config import CONFIG_DIR, DEFAULT_SERVICE_CONFIGS
from common.safe_files import read_json, write_json_atomic

last_good_config = {}


def config_path(service_name):
    return os.path.join(CONFIG_DIR, service_name + ".json")


def load_config(service_name):
    config = dict(DEFAULT_SERVICE_CONFIGS[service_name])

    data = read_json(config_path(service_name))
    if data is None:
        if service_name in last_good_config:
            return last_good_config[service_name]
        return config

    for key in config:
        if key in data:
            config[key] = data[key]

    last_good_config[service_name] = config
    return config


def write_config(service_name, config):
    full = dict(DEFAULT_SERVICE_CONFIGS[service_name])
    for key in config:
        full[key] = config[key]
    write_json_atomic(config_path(service_name), full)


def reset_all_configs():
    for service_name in DEFAULT_SERVICE_CONFIGS:
        write_config(service_name, DEFAULT_SERVICE_CONFIGS[service_name])


def do_work(config):
    latency_ms = random.uniform(config["min_latency_ms"], config["max_latency_ms"])
    time.sleep(latency_ms / 1000.0)

    should_fail = random.random() < config["failure_rate"]
    return should_fail, latency_ms
