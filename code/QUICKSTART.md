# QUICKSTART: commands for demo day

Only the commands, in demo order. `docs\DEMO_PROCEDURE.md` (in the main project folder) explains every command, output and figure.
Use `python` on Windows (`py` if `python` opens the Store); `python3` on Mac/Linux.
Every analysis command prints its tables and **opens its figure** (saved in `results\figures\`).
Add `--no_show` to any of them to save the figure without opening it.

## 0. Install and test (once)

```
cd code
python -m pip install -r requirements.txt
python -m unittest discover tests
```
Ends with `Ran 58 tests ... OK`.

## 1. Terminal 1: start the services (leave it running)

```
python run_all.py
```

## 2. Terminal 2: run 1, normal retries

```
python scenario_runner.py --scenario_id demo1 --mode gradual --fault latency --queue_wait 10 --normal_sec 10 --ramp_sec 10 --hold_sec 8 --recovery_sec 25
python analyze_run.py demo1
```
Figure: `run_demo1.png`.

## 3. Terminal 2: detection, prediction, metrics (no services needed)

```
python labeling.py
python prediction_model.py
python show_metrics.py
```
Figures: `detection_dataset.png`, `prediction.png`, `metrics.png`.

## 4. Terminal 3 then Terminal 2: run 2, predictive controller on

Terminal 3:
```
python controller.py --mode predictive --duration_sec 90
```
Terminal 2, straight after:
```
python scenario_runner.py --scenario_id demo2 --mode gradual --fault latency --queue_wait 10 --normal_sec 10 --ramp_sec 10 --hold_sec 8 --recovery_sec 25
python analyze_run.py demo2
```
Figures: `run_demo2.png`, and `controller_predictive.png` when the controller stops.

## 5. Optional (no services needed)

```
python experiment_arms.py --summary_only
python evaluate_model.py
python graph_builder.py
python validate_data.py
```
Figures: `experiments.png`, `evaluation.png`, `graph_snapshots.png`.

## Notes

- A run name can be used again (the newest run with that name is analysed), but new names keep things clear.
- Rehearse with other names (`p1`, `p2`) so `demo1` and `demo2` are fresh on the day.
- Stop the services with Ctrl+C in Terminal 1.
- Everything from scratch (about 1.5 hours): `python run_pipeline.py`.
