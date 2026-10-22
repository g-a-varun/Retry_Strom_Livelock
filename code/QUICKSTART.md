# QUICKSTART: commands for demo day

README.md explains the project. This file is only the commands, in order.
Commands use `python`; on Mac/Linux type `python3` instead.

## Step 0: install (once)

```
cd code
pip install -r requirements.txt
python -m unittest discover tests
```

The last line runs the test suite (50 tests, under a second). It should end with `OK`.

## Step 1: start the services (Terminal 1, leave it open)

```
python run_all.py
```

Expected:

```
starting payment_service on port 5001
...
gateway is up
all services started. press Ctrl+C to stop.
```

Quick check in a browser: http://127.0.0.1:5000/checkout should say `checkout complete`.

## Step 2: show one self-sustaining livelock live (Terminal 2, about 1 minute)

```
python scenario_runner.py --scenario_id live1 --mode gradual --fault latency --queue_wait 10 --normal_sec 10 --ramp_sec 10 --hold_sec 8 --recovery_sec 25
python analyze_run.py live1
```

What to point at in the `analyze_run` table:
- **recovery** row: the fault is gone, but success stays near 0%. The line under the table
  ("LAST 5 seconds") is also near 0%: the system never came back.
- **calls/succ**: the system works harder and harder per useful result.
- **orphaned%**: work done for requests nobody was waiting for any more.

## Step 3: the same fault with the controller switched on (Terminal 2 + Terminal 3)

Terminal 3, start this first:
```
python controller.py --mode predictive --duration_sec 70
```
Terminal 2, straight after:
```
python scenario_runner.py --scenario_id live2 --mode gradual --fault latency --queue_wait 10 --normal_sec 10 --ramp_sec 10 --hold_sec 8 --recovery_sec 25
python analyze_run.py live2
```
Terminal 3 prints each decision as it happens. `CUT` means the retry budget on that edge was reduced, and `RESTORE` means a step was given back.
Point at the first `CUT` on `gw->order`: it happens about 3 s into the ramp, while `rho` is still `0.00`. That's the pre-emptive cut.

In live2's table, the **recovery** row is an average over the whole 25 s, including the ~15 s spent draining the backlog, so it shows roughly 20–50% (live1 showed ~1-2%). The **LAST 5 seconds** line shows where it ended up: about **100%**, against near 0% for live1.

(`--mode predictive` needs `results/model.json`, which the full pipeline creates. If it's missing, use `--mode reactive`.)

## Step 4: show the saved results

Everything below is already in `results/` from the full run:

| File | What it shows |
|---|---|
| `results/RESULTS.md` | every number in one place |
| `results/experiment_summary.txt` | 5 retry strategies x 2 faults, mean +/- std over 5 runs |
| `results/prediction_results.txt` | model vs baselines, lead time, ablations |
| `results/model_evaluation.txt` | every metric of the model (MCC, ROC-AUC, calibration, 95% intervals, cost) |
| `results/data_validation.txt` | why the generated data can be trusted |
| `results/sensitivity.txt` | what happens if each threshold is moved |
| `results/figures/*.png` | all the figures |

To show the confusion matrix and the accuracy, with every metric worked out step by step in plain
Python (no services needed, about a second):

```
python show_metrics.py
```

To show the full model evaluation live (no services needed, about 45 seconds):

```
python evaluate_model.py
```

It prints every metric (MCC, ROC-AUC, calibration, 95% intervals, run-time cost) and redraws
`results/figures/fig5c_model_evaluation.png`.

## Step 5 (optional): re-run everything from scratch

Stop Terminal 1 first (Ctrl+C), then:

```
python run_pipeline.py --quick      (about 20 minutes, smaller)
python run_pipeline.py              (the full run, about 1.5 hours)
```

It starts and stops its own services and regenerates every file in `results/`. The previous run is copied to `results_previous/` and `data_previous/` first, so nothing is lost.

## If something goes wrong

**"ERROR: these ports are already in use"**: an old copy of the services is still running. Close that terminal, or end `python.exe` in Task Manager, then start again.

**"error: the gateway is not answering"**: Step 1 isn't running.

**"waiting for the previous run's leftover requests to finish..."**: this is normal after a long-queue run. Abandoned requests are still draining. It starts by itself within about 20 s.

**"error: scenario id 'live1' was already used"**: every run needs a new name, so two runs never get mixed up. Use `live3`, `live4`, and so on.

**You pressed Ctrl+C in the middle of a run**: that's fine. The fault is removed automatically, the run is marked "interrupted", and the analysis ignores it. Starting the services again also resets every config to healthy.

**`ModuleNotFoundError: No module named 'flask'`**: Step 0 didn't run, or ran on a different Python. Try `python -m pip install -r requirements.txt`.

**Clean slate**: delete the `data` folder. The services recreate it.
