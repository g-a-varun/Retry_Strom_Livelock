# Code walkthrough: follow one request, then one analysis

Read this with the code open. It follows the data the way it flows: a user request goes
in, telemetry comes out, and the analysis turns telemetry into labels, a model and
decisions. For each file: what it is for, its key functions, and one line to say if asked.

---

## Part 1: the running system

### `common/config.py`: every setting in one place
Ports, the call graph (`EDGES`, `PARENT_EDGE`, `CHILD_EDGES`), healthy service settings,
slot counts, and the default retry policy (3 attempts, 100 ms backoff, 2 s timeout).
*Say:* "Everything is built from these constants, so the scripts work from any folder and
nothing is hard-coded twice."

### `run_all.py`: starts the 4 services
- `start_services()`: refuses to start if a port is busy, resets every config and policy to
  healthy, launches each service as a separate Python process, and waits for `/health`.
- `wait_until_idle()`: polls each service's `/stats` until nothing is in flight. Used between
  runs so leftover work never leaks into the next run.

*Say:* "Each service is its own process, like a real microservice."

### `services/payment_service.py` (inventory is identical)
- `worker_slots = threading.Semaphore(2)`: **2 slots = capacity**.
- `pay()` runs `acquire(timeout=queue_wait)`. With no slot in time it returns **503 busy**.
  With a slot it calls `do_work()` (sleep for the configured latency, then fail with the
  configured probability), releases the slot, and logs one server record (queue wait, service
  time, whether it was orphaned).

*Say:* "A counting semaphore is the worker pool. A request holds a slot for its whole duration,
so retries really compete with fresh requests."

### `services/order_service.py`
Same slot logic with 8 slots. The queue wait comes from the config: 1 s or 10 s. After
its own work it calls payment, then inventory, **each through `call_with_retry`**.
It does not check whether the gateway is still waiting, just like most real services.
*Say:* "This is where orphaned work happens: it keeps working on requests the gateway already gave up on."

### `services/gateway.py`
No slots. Calls order_service through `call_with_retry`.

### `common/retry_client.py`: the retry loop
- `call_with_retry()`: reads the edge's policy, then loops: send the attempt with an
  `X-Deadline` header, record the outcome (code 0 = timeout), decide whether to retry, log the
  attempt, sleep the backoff, try again.
- `backoff_with_jitter_ms()`: 100, 200, 400 ms, with half of each delay random (equal jitter).
- `budget_deposit()` / `budget_take()`: the token bucket for the retry-budget baseline. Every
  fresh call adds 0.1 tokens and every retry costs 1, so retries stay at or below 10% of fresh calls.

*Say:* "Every attempt is logged with its attempt number and a call id. That is the telemetry no
public dataset has."

### `common/retry_policy.py`: the policy file
`get_retry_policy()` reads `data/retry_policy.json` on **every call**, so the controller's
changes take effect immediately. If the file is half-written it uses the last good version;
nonsense values (0 attempts, text) are cleaned.

### `common/fault_injection.py`
`load_config()` reads `configs/<service>.json` on every request, so a fault applies instantly.
`do_work()` is the sleep plus the failure roll.

### `common/safe_files.py`
`write_json_atomic()` writes a temp file, then `os.replace` swaps it in, which is atomic. On
Windows it retries if another process has the file open at that instant.
*Say:* "Several processes share these files. Without atomic writes, a reader can see half a file."

### `common/telemetry.py`: the logs
- `log_call()`, `log_server_request()`, `log_user_request()`, `log_scenario()`: one JSON line each.
- `read_jsonl()`: skips damaged lines (for example after a Ctrl+C) and counts them.
- `read_new_call_records()`: for the controller. It remembers the byte offset in each file
  and reads only new lines, holding back an unfinished last line.
- `within_run()`: keeps only records from one run's time window (protects against a re-run
  of an interrupted scenario).

### `scenario_runner.py`: one experiment run
- `build_schedule()`: normal, then ramp steps (or a sudden fault), then hold, then recovery.
- `traffic_loop()`: **open-loop Poisson arrivals**. One thread per user request, tagged with
  the current phase. It never pauses between phases.
- `run_scenario()`: walks the schedule and writes the fault config at each step. Inside
  `finally` it always restores healthy settings, then records the manifest with exact phase
  times. On Ctrl+C the run is marked interrupted.

*Say:* "Only the fault is scripted. The storm is not."

---

## Part 2: turning telemetry into knowledge

### `features.py`: all the maths, used offline AND live
- `window_stats_for_edge()`: splits one edge's calls into 2 s windows and computes:
  - fresh calls/s and retries/s
  - **rho** = retries ÷ fresh calls
  - useful throughput
  - calls in flight (from exact time overlaps)
  - success fraction, fresh-call failure ratio, overload ratio (503s + timeouts), mean latency
- `reactive_alarm()`: rho > 1 AND overload > 20%, meaning amplification plus saturation.
- `make_features()`: the model's inputs:
  - this edge's values
  - the change since the previous window (temporal)
  - the worst values among the edges below it and the edge above it (**graph message passing**)
- `risk_probability()`: the model by hand, a weighted sum and then a sigmoid.

*Say:* "Labels, training and the live controller all use this one file, so the model sees at
runtime exactly the numbers it was trained on."

### `generate_dataset.py`
Draws 24 random scenarios from a seeded generator, adds 4 no-fault control runs, runs each
with the default retry policy, and waits for the system to go idle between runs. Re-running
continues where it stopped.

### `labeling.py`: Module 4, detection
- `edge_baseline()`: the edge's healthy level, as the median of its first 8 s.
- `check_conditions()`: **C1–C4** (see README section 8).
- `label_scenario()`: livelock needs all four for 2 windows in a row.
- `summarize()`: per edge and phase, false alarms, and what users experienced in flagged windows.
- `run_sensitivity()`: moves each threshold up and down.

### `graph_builder.py`: Module 5
Builds one NetworkX graph per window per scenario (edges carry rho, throughput, label, risk),
saves them, and draws the healthy, early warning and livelock picture.

### `prediction_model.py`: Module 6
- `build_examples()`: features at window *i*, label = livelock anywhere in windows *i+1 … i+5*.
- `cross_validate()`: `GroupKFold` by scenario, then standardise and fit, scoring each example
  with a model that never saw its scenario.
- Baselines (the reactive rule and persistence), the shuffled-label control, the ablations, and
  the random forest.
- `lead_times()`: for each livelock episode, how early the first alarm came.
- At the end it trains on all the data and saves `results/model.json` (weights, means, stds,
  baselines), then self-checks the hand computation against scikit-learn.
- It also saves every method's out-of-fold scores in `results/prediction_oof.csv`.

### `evaluate_model.py`: full evaluation of Module 6
Reads `prediction_oof.csv` and writes `results/model_evaluation.txt`, `.json` and
`figures/fig5c_model_evaluation.png`.
- `threshold_metrics()`: TP/FP/TN/FN and everything from them (accuracy, balanced accuracy,
  precision, recall, specificity, F1, F2, MCC, kappa), written out by hand and self-checked
  against scikit-learn.
- `ranking_metrics()`: ROC-AUC and PR-AUC. `calibration()`: Brier score, log loss, ECE and the
  reliability table.
- `bootstrap()`: 95% intervals. It resamples whole scenarios (`resample_scenarios()`), because
  windows from the same run are not independent.
- `sweep()`: the threshold from 0.1 to 0.9, with lead times and healthy-traffic alarms.
- `runtime_cost()`: times the hand-written risk, a whole `controller.control_step()` round, the
  scikit-learn model and the random forest; also training time and model size.
- Section 8 repeats the shuffled-label control with 20 seeds (`cross_validate(..., shuffle_seed)`).

### `controller.py`: Module 7
- `congestion_starting()`: is the callee side filling up? Overload ≥ 10% on this edge or below
  it, or the callee's response time at least doubled. For gateway→order it looks at order's
  callees, because order's own response time includes its retry back-off sleeps.
- `decide_risky()`: reactive means the reactive rule. Predictive means (model risk ≥ 0.5 AND
  congestion starting) OR the reactive rule.
- `control_step()`: the whole decision for one tick:
  - work out each edge's risk (reactive rule, or model plus rule)
  - collect the edges to cut, adding each risky edge's **parent** (path rule)
  - cut if not in cooldown; otherwise count overload-free ticks and restore one step after
    3 healthy ticks and 4 s since the last cut
- `main()`: every second, read new log lines, compute stats for the last 2 s, run
  `control_step()`, write the policy if it changed, and log the decision. On exit it puts the
  original policy back.

*Say:* "Cut on congestion, restore on its absence, never on rho, because rho is zero by
definition once retries are off."

### `experiment_arms.py`
Runs 5 strategies × 2 faults × 5 repetitions in shuffled order, then computes from the logs:
- success per period
- calls, and calls per success
- orphaned %
- p95 latency
- **recovery time**

It also produces mean ± std and Welch t-tests.

### `validate_data.py`
Checks the generated data against bookkeeping, the retry rules, queueing theory, fault
fidelity, load fidelity and timestamp sanity.

### `make_figures.py`, `run_pipeline.py`, `analyze_run.py`, `calibrate_from_trace.py`
Figures; the one-command full run (with backups); a one-run phase table; and an optional
comparison with the Alibaba production trace (not used in any result).

### `tests/test_core.py`
50 unit tests. Point at these when asked "what about edge cases?":
- empty windows
- only retries
- the rho cap
- overlap maths
- all four conditions, including "overload without retries is not livelock"
- the TAU_LOCK persistence rule
- the controller's no-flapping case
- predictive mode: a pre-emptive cut while rho is still 0, and no cut on fast errors
- cooldown and slow restore
- policy-file corruption
- atomic writes
- the retry loop with fake HTTP
- budget arithmetic
- jitter bounds
- the tail reader with half-written lines and file resets
- the Review-2 command still working
- recovery-time and lead-time maths
- the evaluation maths (MCC / kappa by hand, calibration error, scenario bootstrap)
- the Alibaba `.tar.gz` reader
