# Demo procedure (final review)

This is the plan for showing the project **by running the code**, and what to say about every command and every number it prints. Ma'am's points and where they are covered:

| Ma'am's point | Where |
|---|---|
| **"Matrix": be clear what we used, not library names** | Step 7 (confusion matrix and every metric computed by hand, no library), Section 4 (technique vs library table) |
| **Accuracy must be above 70%** | Steps 6-7: **94.1%** accuracy on runs the model never saw (precision 80.7%, recall 79.2%, balanced accuracy 87.9%, MCC 0.765) |
| **Be clear about the code, even if AI helped: what we did and how it gives the output** | every step below has "How the code does it" and "Output, line by line"; Step 10 is the code tour; Section 6 |
| **No PPT, only the demo** | this procedure: about 15 minutes, every command prints clean tables and opens its graph |

---

## 0. What changed in the code (read once)

- **Clean output.** Every analysis command prints only tables of values and metrics (no explanations), then saves its graph to `results\figures\` and **opens it automatically**. Add `--no_show` to save without opening.
- **Better model (precision 68% → 80.7%).** Two new features (useful-throughput ratio, change in calls in flight), logs of the heavy-tailed ratios (18 features now), and the decision threshold chosen by **nested cross-validation** instead of a fixed 0.5. Accuracy 91.8% → 94.1%, MCC 0.71 → 0.765, false alarms 86 → 41, healthy-traffic alarms 8 → 0; still warns before all 30 episodes, 8 s ahead. Recall went from 84.7% to 79.2%: say this if asked.
- **The 50 experiment runs were re-run with the new model** (results in Step 9).
- **No comments in the code.** Explanations live in this file and `PROJECT_EXPLAINED.md`.
- **Faster:** the log reader skips other runs' lines before parsing them (`analyze_run.py` takes about 1 s instead of several), and 58 unit tests (was 50).
- The **report and the slides still show the first model's numbers** (91.8% accuracy, 68% precision). In the demo, quote what the programs print. If asked: "we improved the model after the report: two new features and a properly chosen threshold halved the false alarms."

---

## 1. Before the day

### The night before (on the laptop you'll present from)

1. PowerShell in the `code` folder:
   ```
   cd X:\Projects\Retry_Strom_Livelock\code
   python --version
   python -m pip install -r requirements.txt
   python -m unittest discover tests
   ```
   Python 3.9 or newer. If `python` opens the Microsoft Store, use `py` everywhere. The tests must end with `Ran 58 tests` and `OK`.
2. **Rehearse the whole demo once** with run names `p1` and `p2` (keep `demo1`, `demo2` for the day; a name can be reused, but fresh names are cleaner).
3. Run `python prediction_model.py` once: it must print **0.941 / 0.807 / 0.792 / 0.799** for logistic regression. If your scikit-learn gives slightly different numbers, that's a version difference; quote what it prints.
4. Note your timings. Your laptop isn't the machine we rehearsed on, so live runs will be close to our numbers, not identical. What must hold: run 1 ends near **0 users served in the last 5 s**; run 2 ends near **95-100%**; run 2's first `CUT` comes while ρ is still near 0.

### 20 minutes before

- Close anything using ports 5000-5003 and any old Python windows.
- Open **three terminals** side by side, all in the `code` folder, big font.
- Open **VS Code** on the `code` folder with these tabs: `services\order_service.py`, `common\retry_client.py`, `common\telemetry.py`, `features.py`, `labeling.py`, `prediction_model.py`, `controller.py`, `show_metrics.py`.
- Close each figure window after explaining it, so the screen stays clean.

---

## 2. Running order (about 15 minutes)

| # | Step | Terminal | Time | Output | Figure |
|---|---|---|---|---|---|
| 1 | Unit tests | 2 | 10 s | 58 tests OK | - |
| 2 | Start the services | 1 | 10 s | 4 processes up | - |
| 3 | Run 1, normal retries | 2 | 1 min | live phase log | - |
| 4 | Report of run 1 | 2 | 2 s | phase table | `run_demo1.png` |
| 5 | Livelock detection | 2 | 3 s | livelock table | `detection_dataset.png` |
| 6 | Prediction model | 2 | 10 s | 3 tables | `prediction.png` |
| 7 | Confusion matrix and accuracy | 2 | 1 s | 3 tables | `metrics.png` |
| 8 | Run 2 with the controller | 3 + 2 | 1.5 min | decisions + phase table | `run_demo2.png`, `controller_predictive.png` |
| 9 | Extras, if asked | 2 | - | experiments, full evaluation | `experiments.png`, `evaluation.png` |
| 10 | Code tour | VS Code | 2-5 min | - | - |
| 11 | Close | - | 30 s | - | - |

**Opening (1 min, before Step 1), say:** "Ma'am, when a microservice gets slow, its callers time out and retry. The retries add load to the service that is already struggling, so more calls fail and more retries go out. The system ends up 100% busy while almost no request succeeds: livelock. We built a real 4-service system where this happens, a formal test that detects it, a model that predicts it about 8 s early, and a controller that cuts retries before the storm forms. I'll run each part and explain the output."

---

## 3. The demo, step by step

### Step 1. Unit tests (Terminal 2, 10 s)

**Run:** `python -m unittest discover tests`

**Output:**
```
..........................................................
----------------------------------------------------------------------
Ran 58 tests in 0.02s

OK
```

**What it does:** runs 58 small checks on the parts of the system without starting any service: window statistics, the four livelock conditions, the controller's cut/restore rules, the policy file, the retry client, the log reader, the scenario schedule, the features, the threshold search and every metric formula.

**Why:** each part can be trusted on its own before we show the whole system. Each dot is one test passing.

**Say:** "Before running anything live, these 58 tests check each part on its own: the four livelock conditions, the controller rules, the retry client, the features and the metric formulas. All pass."

---

### Step 2. Start the four services (Terminal 1, leave it running)

**Run:** `python run_all.py`

**Output:** `starting ... on port 500x` for each service, `... is up`, then `all services started. press Ctrl+C to stop.`

**How the code does it:** `run_all.py` starts **4 separate OS processes** (`subprocess.Popen`), one Flask server each: gateway :5000, payment :5001, inventory :5002, order :5003. It waits until each answers `/health`.

**Why separate processes:** a real microservice system is separate processes talking over the network. A slow service can't hide behind shared memory, and every call really goes through HTTP with a timeout.

**Say:** "Four OS processes, real HTTP between them. The gateway calls order; order calls payment and inventory. Order has 8 worker slots, payment and inventory 2 each: these are counting semaphores."

---

### Step 3. Run 1: the fault with normal retries (Terminal 2, about 1 minute)

**Run:**
```
python scenario_runner.py --scenario_id demo1 --mode gradual --fault latency --queue_wait 10 --normal_sec 10 --ramp_sec 10 --hold_sec 8 --recovery_sec 25
```

**Output:** one line per phase change (`[ 10.0s] ramp: payment_service -> {'min_latency_ms': 164, ...}`), then `done: N user requests sent`.

**What it does:** sends 20 users per second (random Poisson arrivals) to the gateway for 53 s. 0-10 s healthy; 10-20 s payment's latency ramps up to 700-900 ms in 5 steps; held until 28 s; then the fault is **removed** and we watch 25 s of "recovery". Order's slot wait is 10 s (long queue).

**How:** the fault injector writes `configs/payment_service.json`; payment re-reads it on every request. Only the fault is injected; retries, queueing and collapse happen by themselves. Every call attempt, every received request and every user is written as one JSON line to `data/*.jsonl`.

**Say while it runs:** "Only payment is slowed down. Watch what happens after 28 s, when the fault is removed."

---

### Step 4. Report of run 1 (Terminal 2, 2 s)

**Run:** `python analyze_run.py demo1`

**Output (example from our rehearsal):**
```
RUN demo1  |  gradual latency fault on payment_service  |  20 users/s  |  order slot wait 10 s

Phase      Time(s)  Users  Served% Goodput/s   Latency   Calls   Retry%  Calls/OK   Orphan%
-------------------------------------------------------------------------------------------
normal        0-10    191    100.0      19.1     205ms     594      4.4       3.1       0.0
ramp         10-20    196      9.7       1.9    5743ms     546     44.9      28.7      64.3
fault        20-28    148      0.0       0.0    6237ms     526     63.5     526.0     100.0
recovery     28-53    526      2.1       0.4    6157ms    3132     36.3     284.7      98.5

Last 5 s: 1/80 users served (1.2%)
Figure: results\figures\run_demo1.png
```
A figure opens: `results\figures\run_demo1.png`.

**The columns:**

| Column | Meaning | How it's computed |
|---|---|---|
| Phase, Time(s) | the phases the scenario runner recorded | from `data/scenarios.jsonl` |
| Users | users who arrived in that phase | count of `user_requests.jsonl` lines |
| Served% | users who got a successful checkout | successes ÷ users |
| Goodput/s | successful users per second | successes ÷ phase length |
| Latency | average time a user waited | mean of `total_time_ms` |
| Calls | every internal call attempt (all edges, first tries and retries) | count of `telemetry_*.jsonl` lines |
| Retry% | share of those calls that were retries | attempt number > 1 |
| Calls/OK | internal calls spent per successful user | Calls ÷ successes |
| Orphan% | share of order_service's finished work that finished **after** the gateway had already given up | `orphaned = done_time > deadline` (`common/telemetry.py`) |
| Last 5 s | users served in the last 5 seconds of the run | where the system ended up |

**What to point at:** the **recovery** row. The fault is gone, but almost nobody is served, the system makes hundreds of calls per success, and almost all of order's work is orphaned (done for nobody). **Last 5 s: 0 or nearly 0 served.** That is the self-sustaining livelock: busy, but no progress.

**The figure:**
- **(a) Users served:** 100% while healthy, falls to ~0 in the ramp, and **stays at 0 after the fault is removed** (the shaded band is the fault).
- **(b) Internal calls:** green = first attempts, orange = retries. Retries grow during the fault and the total stays high after it: the system works harder than when it was healthy.
- **(c) order_service:** slots 100% busy and orphaned work near 100%: every slot is spent on requests nobody is waiting for.

**Why the storm outlives the fault (say this):** "After the fault, order can finish about 32 requests per second: 8 slots, about a quarter second each. 20 users per second with 3 attempts can bring up to 60 per second. 60 is more than 32, so the queue never drains. The gateway gives up after 2 s, but the abandoned request stays in order's 10 s queue and is processed later for nobody. That's the orphan column."

---

### Step 5. Livelock detection on the recorded dataset (Terminal 2, 3 s)

**Run:** `python labeling.py`

**Output:**
```
LIVELOCK DETECTION  |  group dataset  |  28 runs  |  1878 windows (edge x 2 s)  |  131 livelock

Table 1. Livelock windows / windows, by edge and phase
Edge                     Normal       Ramp      Fault   Recovery
----------------------------------------------------------------
gateway->order            0/212       8/93     46/104     63/209
order->payment            0/212       0/93      1/104      0/209
order->inventory          0/212       1/93      9/104      0/209

False alarms (normal phase): 0/636 windows
User success: livelock windows 9.0%  |  other ramp/fault/recovery windows 68.1%

Labels: data\labeled_windows.csv
Figure: results\figures\detection_dataset.png
```
A figure opens: `results\figures\detection_dataset.png`.

**What it does:** cuts the 28 recorded runs into 2-second windows per call edge (1,878 windows) and labels each one livelock or not with our formal definition, two windows in a row:
- **C1** ρ > 1 (more retries than fresh calls)
- **C2** S < 0.5·S\* (useful throughput below half of this edge's healthy level)
- **C3** N > max(2·N\*, 1) and not falling (calls in flight piled up and not draining)
- **C4** retries not falling and success not improving (the loop isn't dying out)

S\* and N\* are this edge's own healthy levels (median of its first 8 s), so every edge is judged against itself.

**How:** `features.py → finish_window()` turns raw call lines into ρ, S, N, P per window; `labeling.py → check_conditions()` applies C1-C4; `label_scenario()` requires 2 windows in a row.

**Output, line by line:**
- **Table 1:** livelock windows / all windows, per edge and phase. **0 in every normal phase** (0/636 false alarms, including two high-load runs with no fault). Almost all livelock is on **gateway→order**, during the fault and the recovery.
- **User success:** users who arrived in a livelock window were served 9.0% of the time, against 68.1% in other bad windows. The label matches what users felt.

**The figure:** (a) livelock windows per phase and edge; (b) the 9.0% vs 68.1% user success; (c)-(e) one run (ds_06, gateway→order) over time: ρ crosses 1 (C1), useful throughput drops below half of healthy (C2), calls in flight rise far above 2× healthy (C3). The red band is where all four hold: livelock, and it **continues after "fault off"**.

**Why C1 matters:** without retries a system can be overloaded and useless, but that is plain overload, not livelock. C1 and C4 separate retry livelock from overload.

**Why the storm is on gateway→order, not payment:** order has only 8 slots, which caps how many retries can pile up towards payment. The gateway has no limit, so the retries pile up there.

---

### Step 6. Train and test the prediction model (Terminal 2, about 10 s)

**Run:** `python prediction_model.py`

**Output:**
```
Cross-validating 5 models (5 folds grouped by run, nested threshold search)...

LIVELOCK PREDICTION  |  horizon 2-12 s  |  5-fold grouped CV  |  28 runs, 1458 windows, 216 positive (14.8%)

Table 1. All windows
Method                          Acc     Prec   Recall       F1      MCC   PR-AUC
--------------------------------------------------------------------------------
Logistic regression           0.941    0.807    0.792    0.799    0.765    0.853
  w/o graph features          0.924    0.718    0.801    0.757    0.714    0.812
  w/o temporal features       0.940    0.809    0.782    0.795    0.761    0.846
Random forest                 0.941    0.825    0.764    0.793    0.760    0.883
Shuffled labels               0.160    0.130    0.819    0.224   -0.195    0.121
Reactive rule                 0.898    0.715    0.523    0.604    0.556        -
Persistence rule              0.901    0.874    0.384    0.534    0.539        -

Table 2. Onset windows (n = 1341, positive = 123)
Method                          Acc     Prec   Recall       F1      MCC   PR-AUC
--------------------------------------------------------------------------------
Logistic regression           0.950    0.746    0.691    0.717    0.691    0.779
  w/o graph features          0.930    0.604    0.683    0.641    0.604    0.700
  w/o temporal features       0.949    0.741    0.675    0.706    0.679    0.780
Random forest                 0.949    0.765    0.634    0.693    0.669    0.786
Shuffled labels               0.116    0.082    0.854    0.150   -0.139    0.072
Reactive rule                 0.908    0.500    0.236    0.320    0.301        -
Persistence rule              0.908    0.000    0.000    0.000    0.000        -

Table 3. Early warning (30 livelock episodes)
Method                       Warned   Median (s)  Range (s)   Healthy alarms
----------------------------------------------------------------------------
Logistic regression           30/30          8.0       2-10            0/576
Reactive rule                 24/30          2.0       2-10            0/576

Confusion matrix (logistic regression):  TN = 1201   FP = 41   FN = 45   TP = 171
Decision threshold (nested CV, per fold): 0.70, 0.80, 0.75, 0.70, 0.65
Final model: 18 features, threshold 0.65, trained on 1458 windows -> results\model.json
Self-check |p(own code) - p(scikit-learn)| max = 5.6e-16
Figure: results\figures\prediction.png
```
A figure opens: `results\figures\prediction.png`.

**The question it answers:** every 2 s, for every edge: *will this edge be in livelock at some point in the next 2-12 s?* (label = livelock in any of the next 5 windows; the current window is not used, so this is prediction, not detection).

**How:**
1. `features.py → make_features()` builds **18 features** per edge per window: the edge's own state (ρ, fail ratio, overload, log latency ratio, log calls-in-flight ratio, useful-throughput ratio), how it changed since the last window, **graph features** (worst values on the edges below, values of the edge above: one round of message passing), and which edge it is.
2. **Logistic regression:** standardise each feature, weighted sum z = b + Σ wᵢxᵢ, risk p = 1/(1+e^-z).
3. **5-fold cross-validation grouped by run** (`cross_validate()`): each test run is never seen in training, not even its other windows.
4. **Threshold by nested cross-validation** (`best_threshold()`): inside the 4 training folds, a second cross-validation picks the threshold with the best F1 (0.30-0.90). The test runs never influence their own threshold. Per fold it chose 0.70, 0.80, 0.75, 0.70, 0.65.
5. A **final model** is trained on all 28 runs and saved as 56 plain numbers in `results/model.json` (threshold 0.65). The controller computes the risk from these numbers with our own function.

**Output, line by line:**
- **Table 1 (all windows):** our model **accuracy 0.941, precision 0.807, recall 0.792, F1 0.799, MCC 0.765**. Compared with:
  - *w/o graph features*: precision drops to 0.718 → the graph features matter.
  - *w/o temporal features*: almost the same → the changes add little (we say so).
  - *Random forest* (100 trees): F1 0.793, no better than ours, and about 1,900× slower per prediction.
  - *Shuffled labels*: trained on randomly shuffled answers, MCC −0.195 → the setup does not leak the answer.
  - *Reactive rule* (ρ > 1 and overload now) F1 0.604; *persistence* ("livelock soon if livelock now") F1 0.534.
- **Table 2 (onset windows):** only windows where the edge is **not yet** in livelock: the honest prediction test. Ours F1 0.717; reactive rule 0.320; persistence 0.000 (it can't see a storm before it starts).
- **Table 3 (early warning):** our model warned before **30 of 30** livelock episodes, a **median 8 s** early, with **0** alarms in 576 healthy windows. The reactive rule: 24 of 30, 2 s.
- **Self-check:** our hand-written risk formula equals scikit-learn's to 10⁻¹⁶: the controller computes exactly what was trained.

**The figure:** (a) precision, recall, F1 and MCC of every method side by side; (b) how many seconds of warning before each episode; (c) the model's weights (blue pushes towards "livelock coming", orange away).

**Why logistic regression:** it runs every second inside the controller: about 3 µs per edge, 56 numbers, readable weights. **Why grouped CV:** neighbouring windows of one run are near-copies; a random split would leak them.

**How precision went from 68% to 81% (if asked):** the first version raised false alarms in fast error bursts (lots of errors, but users still served, nothing piling up). We added features that see useful work and the build-up directly (useful-throughput ratio, change in calls in flight), used logs for the heavy-tailed ratios, and chose the threshold by nested cross-validation instead of fixing 0.5. False alarms fell from 86 to 41; recall fell from 84.7% to 79.2%, but all 30 episodes are still warned about.

---

### Step 7. Confusion matrix and accuracy, computed by hand (Terminal 2, 1 s)

**Run:** `python show_metrics.py`

**Output:**
```
PREDICTION METRICS  |  1458 out-of-fold windows from 28 runs  |  threshold 0.65-0.80 (nested CV)

Table 1. Confusion matrix
                 Predicted NO  Predicted YES      Total
-------------------------------------------------------
Actual NO           TN = 1201        FP = 41       1242
Actual YES            FN = 45       TP = 171        216
Total                    1246            212       1458

Table 2. Metrics
Metric             Formula                    Computation                                      Value
----------------------------------------------------------------------------------------------------
Accuracy           (TP+TN) / N                (171+1201) / 1458                                94.1%
Precision          TP / (TP+FP)               171 / 212                                        80.7%
Recall             TP / (TP+FN)               171 / 216                                        79.2%
Specificity        TN / (TN+FP)               1201 / 1242                                      96.7%
Balanced accuracy  (Recall+Specificity) / 2   (0.792+0.967) / 2                                87.9%
F1                 2PR / (P+R)                2(0.807)(0.792) / 1.598                          79.9%
MCC                (TP.TN-FP.FN) / sqrt(...)  (171x1201-41x45) / sqrt(212x216x1242x1246)       0.765
ROC-AUC            P(risk+ > risk-)           268272 pairs                                     0.954
PR-AUC             sum dRecall x Precision    1458 windows                                     0.853

Table 3. Comparison
Method                      Acc   BalAcc     Prec   Recall       F1      MCC
----------------------------------------------------------------------------
Logistic regression       94.1%    87.9%    80.7%    79.2%    79.9%    0.765
Reactive rule             89.8%    74.3%    71.5%    52.3%    60.4%    0.556
Always NO                 85.2%    50.0%     0.0%     0.0%     0.0%    0.000

Figure: results\figures\metrics.png
```
A figure opens: `results\figures\metrics.png`.

**What it does:** reads the out-of-fold predictions (`results/prediction_oof.csv`: each window's risk from a model that never saw its run, the threshold chosen without its run, and the alarm). Then, in **plain Python with no library**:
- `count_cells()` loops once over the 1,458 windows and counts TP, FP, TN, FN.
- `metrics_from_cells()` applies the formulas in Table 2 (even the square root in MCC is our own Newton's method).
- `roc_auc_by_hand()` checks all 268,272 (livelock, no-livelock) pairs: in how many did the livelock window get the higher risk?
- `pr_auc_by_hand()` walks down the windows from highest risk and adds up precision × recall gained.

**Output, line by line:**
- **Table 1:** 1,242 windows were really "no livelock coming" and 216 "yes". The model said yes 212 times: 171 right (TP), 41 false alarms (FP). It missed 45 (FN).
- **Table 2:** each metric, its formula, the numbers put in, the result. **Accuracy (171+1201)/1458 = 94.1%.**
- **Table 3:** why accuracy alone isn't enough: "always say NO" already gets 85.2% accuracy but 0% recall and MCC 0. Balanced accuracy (87.9%) and MCC (0.765) are the fair numbers. The reactive rule: 89.8% accuracy but only 52.3% recall.

**The figure:** (a) the confusion matrix (green = correct, red = mistakes); (b) the ROC curve, AUC 0.954 (random guessing is the diagonal); (c) the precision-recall curve, AP 0.853 (random is 0.148). The orange dot is where our threshold puts us. Every point on both curves is computed by our own code; matplotlib only draws it.

**Say:** "Ma'am, this is the accuracy calculation. Every number comes from these four counts, computed here without any library."

---

### Step 8. Run 2: the same fault with our predictive controller (Terminals 3 and 2, about 1.5 minutes)

**Terminal 3 first:** `python controller.py --mode predictive --duration_sec 90`

**Then immediately in Terminal 2** (same command as run 1, new name):
```
python scenario_runner.py --scenario_id demo2 --mode gradual --fault latency --queue_wait 10 --normal_sec 10 --ramp_sec 10 --hold_sec 8 --recovery_sec 25
python analyze_run.py demo2
```

**Controller output (Terminal 3), one line per second:**
```
CONTROLLER  |  mode predictive  |  90 s  |  every 1 s  |  risk threshold 0.65
...
t=+  14s | gw->order rho 0.00 risk 0.62 -> 3 | order->pay rho 0.03 risk 0.04 -> 3 | order->inv rho 0.07 risk 0.08 -> 3
t=+  15s | gw->order rho 0.00 risk 0.91 -> 2 CUT | order->pay rho 0.12 risk 0.06 -> 3 | order->inv rho 0.00 risk 0.10 -> 3
t=+  16s | gw->order rho 0.00 risk 0.75 -> 2 | order->pay rho 0.15 risk 0.01 -> 3 | order->inv rho 0.07 risk 0.07 -> 3
t=+  17s | gw->order rho 0.00 risk 0.76 -> 2 | order->pay rho 0.07 risk 0.00 -> 3 | order->inv rho 0.10 risk 0.04 -> 3
t=+  18s | gw->order rho 0.24 risk 0.88 -> 1 CUT | order->pay rho 0.10 risk 0.00 -> 3 | order->inv rho 0.00 risk 0.02 -> 3
t=+  19s | gw->order rho 0.64 risk 0.75 -> 1 | order->pay rho 0.25 risk 0.00 -> 3 | order->inv rho 0.00 risk 0.02 -> 3
...
t=+  24s | gw->order rho 0.08 risk 0.00 -> 1 | order->pay rho 1.75 risk 0.00 -> 2 CUT | order->inv rho 0.50 risk 0.07 -> 3
...
t=+  29s | gw->order rho 0.00 risk 0.01 -> 1 | order->pay rho 1.20 risk 0.00 -> 1 CUT | order->inv rho 0.33 risk 0.06 -> 3
...
t=+  34s | gw->order rho 0.00 risk 0.12 -> 1 | order->pay rho 0.00 risk 0.00 -> 2 RESTORE | order->inv rho 0.08 risk 0.01 -> 3
...
t=+  37s | gw->order rho 0.00 risk 0.05 -> 1 | order->pay rho 0.03 risk 0.00 -> 3 RESTORE | order->inv rho 0.09 risk 0.00 -> 3
...
```
After 90 s it prints its summary and opens `results\figures\controller_predictive.png`:
```
Edge             Cuts  Restores   Min attempts  First cut
---------------------------------------------------------
gw->order           2         2              1      t=15s
order->pay          2         2              1      t=24s
order->inv          0         0              3          -
Retry policy restored to the original.
Figure: results\figures\controller_predictive.png
```

**Reading a controller line:** `t=+  15s | gw->order rho 0.00 risk 0.91 -> 2 CUT | ...` means: 15 s after the controller started, on gateway→order, ρ was 0.00, the model's risk was 0.91, and the controller cut the retry budget from 3 to 2 attempts. `RESTORE` gives one attempt back.

**How the controller decides** (`controller.py → control_step()`), every second, for every edge:
1. Read only the **new** lines of the call logs (`read_new_call_records()`, remembered byte offsets).
2. Compute the last 2 s of statistics and the 18 features, and the risk p with our own formula.
3. **Risky** = (p ≥ 0.65 **and** congestion starting below the edge) **or** (ρ > 1 and overload > 20%, the reactive safety net).
4. Risky → **cut**: attempts −1 (min 1), backoff ×2 (max ×4), and also cut the edge above (path rule), at most every 2 s.
5. Calm for 3 checks and 4 s since the last cut → **restore** one step.
6. If anything changed, write `data/retry_policy.json` (temp file + atomic rename); every retry client reads it on its next call.
This is **AIMD**, like TCP: cut fast, give back slowly. On exit it puts the original policy back.

**What to point at:**
- **15 s:** the first CUT on gw->order comes while **ρ is still 0.00**: no storm yet, so no reactive rule could fire. That's the pre-emptive cut. At 18 s it cuts again, to 1 attempt.
- **19-22 s:** ρ on gw->order briefly rises: those are retries of calls that started before the cut. They die out in 3 s.
- **24 s and 29 s:** order->pay gets its own storm (ρ 1.75 with overload): the reactive safety net cuts it.
- **34 s and 37 s:** order->pay has been calm for 3 checks and 4 s, so one attempt at a time is given back (RESTORE). gw->order is given back at about 53 s and 56 s.

**Run 2 report:**
```
RUN demo2  |  gradual latency fault on payment_service  |  20 users/s  |  order slot wait 10 s

Phase      Time(s)  Users  Served% Goodput/s   Latency   Calls   Retry%  Calls/OK   Orphan%
-------------------------------------------------------------------------------------------
normal        0-10    191    100.0      19.1     205ms     594      4.5       3.1       0.0
ramp         10-20    196      8.2       1.6    3215ms     437     31.1      27.3      69.8
fault        20-28    148      0.7       0.1    1995ms     230     10.4     230.0      96.6
recovery     28-53    526     28.7       6.0    1654ms    1978      3.2      13.1      77.8

Last 5 s: 77/80 users served (96.2%)
Figure: results\figures\run_demo2.png
```
- **Last 5 s: 77/80 users served (96%)**, against 1/80 in run 1. In the recovery phase, calls per success fell from 284.7 to 13.1 and retries from 36% to 3% of calls. The recovery row averages the whole 25 s, including the time spent draining the backlog, so look at the last-5-seconds line for where it ended up.

**The controller figure:** (a) the retry budget per edge (▼ = cut, ▲ = restore); (b) ρ per edge: short bumps that die out within seconds instead of a lasting storm; (c) the risk per edge with the 0.65 threshold dashed: the risk on gw->order crosses it at 15 s, before ρ moves at all.

**Compare the two run figures** (`run_demo1.png` and `run_demo2.png`): same fault, same load; in run 2 users come back to 100% after the fault, retries stay small and orphaned work drops.

---

### Step 9. Optional extras (only if she asks; no services needed)

| Command | Time | Output | Figure |
|---|---|---|---|
| `python experiment_arms.py --summary_only` | 5 s | all 50 recorded experiment runs: 5 strategies × 2 faults × 5 runs, mean ± std, Welch t-tests | `experiments.png` |
| `python evaluate_model.py` | 45 s | every metric with 95% bootstrap intervals, calibration, threshold sweep, per-group results, run-time cost | `evaluation.png` |
| `python graph_builder.py` | 2 s | the service graph per window and the message-passing neighbours | `graph_snapshots.png` |
| `python validate_data.py` | 5 s | 12 checks that the generated data behaves like a real system, all PASS | - |

**Experiment results to quote** (`experiment_arms.py --summary_only`, 50 recorded runs, re-run with the current model):

| | Metastable: served after fault | Recovered | Recovery time | Calls in fault | Error burst: served during fault |
|---|---|---|---|---|---|
| No retry | 45.5% | 5/5 | 12.0 s | 662 | 39.0% |
| Static retry | **2.9%** | **0/5** | never | 1,212 | **74.0%** |
| Retry budget 10% | 23.7% | 3/5 | 20.4 s | 682 | 53.1% |
| Reactive (ours) | 40.6% | 5/5 | 14.8 s | 973 | 69.2% |
| **Predictive (ours)** | **48.3%** | **5/5** | **13.0 s** | 676 | **71.1%** |

Say: "No fixed policy is right for both faults: static retries never recover from the metastable fault but are best in the error burst; the budget is the opposite. Predictive is the only one that is never bad: it recovered every time and fastest, and kept 71% in the error burst against 74% for static retries (not a significant difference, p = 0.33). Against the budget it is significantly better in both (p = 0.011 and p < 0.001). The self-sustaining livelock on gateway→order formed in 5/5 static and 5/5 reactive runs, and in 0/5 predictive runs; the predictive controller's first cut came at 11.5 s in every run, the reactive one's at 17.5-19.5 s."

---

### Step 10. Code tour (when she asks to see the code)

Open these in VS Code. About 30 s each; say what it does in your own words.

| # | File : line | What to show | What to say |
|---|---|---|---|
| 1 | `services\order_service.py` : 47 (semaphore made at 96) | `worker_slots.acquire(timeout=config["queue_wait_sec"])` | "The worker slots are a counting semaphore. A request waits at most `queue_wait_sec` for a slot, else 503. The threads blocked here are the queue." |
| 2 | `common\retry_client.py` : 40, 56 (backoff at 35) | `call_with_retry()`, the `while True` loop, `backoff_with_jitter_ms()` | "Each attempt sends a deadline header, waits up to 2 s, logs one line; if it failed and attempts are left, it sleeps a backoff with jitter and tries again." |
| 3 | `common\telemetry.py` : 66 | `orphaned = done_time > deadline` | "This is how we measure wasted work: the service finished after its caller had given up." |
| 4 | `features.py` : 51 | `finish_window()` | "From the attempts that ended in a 2 s window: fresh calls, retries, ρ, useful throughput, calls in flight, overload." |
| 5 | `labeling.py` : 89 | `check_conditions()` | "The four livelock conditions, exactly as defined." |
| 6 | `features.py` : 220 and 276 | `make_features()`, `risk_probability()` | "18 features including the graph ones; the risk is our own weighted sum and sigmoid." |
| 7 | `prediction_model.py` : 193 and 204 | `best_threshold()`, `cross_validate()` | "Grouped 5-fold CV, and a second CV inside the training folds picks the threshold." |
| 8 | `controller.py` : 111 (60, 86, 99) | `control_step()`, `congestion_starting()`, `cut()`, `restore()` | "Risky → cut one attempt and double the backoff, and the edge above too. Calm → give one step back." |
| 9 | `show_metrics.py` : 33 and 68 | `count_cells()`, `metrics_from_cells()` | "The confusion matrix is four counters in a loop; every metric is a formula on those four numbers." |

---

### Step 11. Close (30 s)

**Say:** "To sum up: with normal retries, a slowdown of a few seconds turned into a failure that never ended. Our detector finds these livelocks with zero false alarms. Our model predicts them about 8 seconds ahead, 94.1% accuracy and 80.7% precision on runs it never saw. With our controller on, the same fault recovered by itself. Retries help with short, fast failures and hurt when a service is saturated, so the retry budget has to adapt while the system runs, ideally before the storm forms. Thank you, ma'am."

Then **Ctrl+C in Terminal 1** to stop the services.

---

## 4. What we used in the program (technique first, library second)

If she asks "what did you use?", say the **technique**, then which part is our own code. Never just "we used scikit-learn".

| Part | Technique, in words | Our own code does | A library only does |
|---|---|---|---|
| 4 services | 4 OS processes, one thread per request; worker slots = **counting semaphore** with a timed wait | slots, simulated work, errors, logging, calling the next service | **Flask** receives the HTTP request and gives it a thread |
| Calls | timeout + retry, **exponential backoff with jitter**, deadline header, **token-bucket** retry budget | the whole retry loop | **requests** sends one HTTP call |
| Fault injection | change a running service's latency / error rate through a config file it re-reads on every request | all of it | - |
| Logs | **JSON Lines**: one record per attempt, per request, per user; atomic file replace for the policy file | all of it, including the fast reader that skips other runs' lines | - |
| Detection | 2 s windows per edge; ρ, S, N, P; **four formal conditions C1-C4**, 2 windows in a row | all of it (`features.py`, `labeling.py`) | - |
| Prediction | **logistic regression**: p = 1 / (1 + e^-(w·x + b)), 18 features, graph **message passing**; threshold chosen by **nested cross-validation** | features, the risk formula used at run time, labels, threshold search, lead time | **scikit-learn** finds the 18 weights + bias during training (L-BFGS), mean/std, the 5 folds |
| Metrics | confusion matrix → accuracy, precision, recall, specificity, F1, MCC, ROC-AUC, PR-AUC | all of them, by counting (`show_metrics.py` uses no library at all) | scikit-learn only cross-checks our numbers |
| Controller | **AIMD feedback control** (like TCP): cut fast, give back slowly | all of it (`controller.py`) | - |
| Experiments | mean ± std, **Welch's t-test** | means, std, recovery time, orphaned % | **scipy** computes the p-value |
| Graphs | line, bar and curve plots | every number that is plotted | **matplotlib** only draws |

Our code never imports numpy.

---

## 5. Numbers to know without notes

| What | Number |
|---|---|
| Order capacity after the fault | 8 slots ÷ ~0.25 s ≈ **32 req/s** |
| Load with 3 attempts / with 1 attempt | up to **60/s** (never drains) / **20/s** (drains at 12/s) |
| Dataset | **28 runs**, 1,878 edge-windows, **131 livelock**, **0/636** false alarms |
| User success in livelock windows vs other bad windows | **9.0%** vs **68.1%** |
| Prediction examples | **1,458** windows, **216** positive (14.8%) |
| Confusion matrix | TN **1,201**, FP **41**, FN **45**, TP **171** |
| Accuracy / precision / recall / F1 | **94.1% / 80.7% / 79.2% / 79.9%** |
| Specificity / balanced accuracy / MCC | **96.7% / 87.9% / 0.765** |
| ROC-AUC / PR-AUC | **0.954 / 0.853** (random: 0.5 / 0.148) |
| Early warning | **30/30** episodes, median **8 s** ahead, **0/576** healthy alarms |
| Model | **18 features**, threshold **0.65**, **56 numbers** in model.json |
| Speed | ~3 µs per risk in our code, ~27 µs per controller round (3 edges) |

---

## 6. About the code being AI-assisted

Ma'am said that even if AI helped write the code, we must be clear about what we did and how it produces the output. Our honest position: **we used AI tools as assistants while writing and cleaning up the code; the design, the experiments and the choices are ours, and we understand every part.** Don't deny it if asked directly. Show understanding instead:
- Do the chain "logs → windows → C1-C4 labels → 18 features → logistic regression → risk → cut/restore" without notes.
- Open any stop of the code tour (Step 10) and explain it in your own words.
- Know the 3 numbers behind the storm: **32/s** capacity, up to **60/s** arriving with 3 attempts, **20/s** with 1.
- Know the 4 boxes of the confusion matrix and every formula in `show_metrics.py`.

---

## 7. If something goes wrong

| What you see | What to do |
|---|---|
| `ERROR: these ports are already in use` | close old terminals, or in PowerShell: `netstat -ano \| findstr :500`, then `taskkill /PID <number> /F` |
| `error: the gateway is not answering` | Terminal 1 isn't running: `python run_all.py` |
| `waiting for the previous run's leftover requests...` | normal, wait about 20 s |
| `ModuleNotFoundError` | `python -m pip install -r requirements.txt` |
| A figure window doesn't open | the PNG is still saved: open the path printed after `Figure:` |
| Too many figure windows | add `--no_show` to any command |
| Run 1 recovers (last 5 s well above 0%) | check `--queue_wait 10`; re-run with a new name and `--rps 22` |
| Run 2 doesn't reach about 100% at the end | re-run with new names, `--recovery_sec 35` and controller `--duration_sec 100` |
| Anything else | don't debug for more than 30 s. Use the saved results: `python show_metrics.py`, `python experiment_arms.py --summary_only`, and the PNGs in `results\figures\` |

**Saved results that need no services:** `show_metrics.py`, `prediction_model.py`, `labeling.py`, `evaluate_model.py`, `graph_builder.py`, `validate_data.py`, `experiment_arms.py --summary_only`. Every one of them prints its tables and opens its figure in a few seconds.

---

## 8. Questions she's likely to ask

**"What is the accuracy?"** 94.1% on runs the model never saw. Precision 80.7%, recall 79.2%, F1 79.9%, balanced accuracy 87.9%, MCC 0.765. (`show_metrics.py`, Step 7.)

**"Show me how accuracy is calculated."** `python show_metrics.py`: TP 171, TN 1,201, FP 41, FN 45; accuracy = (171 + 1201) / 1458. Every number is counted in a plain loop (`count_cells()`), no library.

**"Which algorithm did you use?"** Logistic regression on 18 hand-built features, including graph features from one round of message passing; the threshold is chosen by nested cross-validation; tested with 5-fold cross-validation grouped by run. Plus a formal four-condition detector and an AIMD controller.

**"Why logistic regression?"** The controller runs it every second for every edge: about 3 µs per risk in our own code, 56 numbers, readable weights. A random forest is no better (F1 0.79 vs 0.80) and about 1,900× slower.

**"How did you improve precision?"** The first version's false alarms were in fast error bursts: lots of errors but users still served. We added features that see useful work and the build-up directly, used logs of the heavy-tailed ratios, and chose the threshold by nested cross-validation. 86 false alarms → 41; precision 68% → 81%; recall 85% → 79%; still 30/30 episodes warned, 8 s ahead.

**"Did you tune the threshold on the test data?"** No. Inside each training split, a second cross-validation picks the threshold; the test runs never see it. The folds chose 0.65-0.80; the final model 0.65.

**"Where does your data come from?"** We generated it by running the real 4-service system and injecting only the fault: 28 runs for training, 50 for the experiments. No public dataset has per-attempt retry data with fault timing. `validate_data.py`: 12 checks, all pass.

**"How do you know the model isn't cheating?"** Each run is scored by a model trained without that run, with a threshold chosen without that run. A model trained on shuffled labels gets MCC −0.2. Our metric formulas match scikit-learn to 10⁻¹⁶.

**"What does this output mean?"** Use "Output, line by line" under each step.

**"What's new compared with existing work?"** A formal per-edge test that separates retry livelock from plain overload; predicting it 2-12 s ahead using neighbouring edges in the call graph; a controller that acts on that prediction per edge, in steps, before the storm forms; measuring wasted (orphaned) work directly.

**"Where is the OS?"** Livelock, counting semaphores, bounded waiting, processes and threads, IPC through files, atomic rename, the Utilization Law and Little's Law, AIMD feedback control.

**"Is the controller always right?"** No. An unneeded cut costs a few requests that a retry would have saved. That's why it gives retries back automatically, why the reactive safety net stays on, and why predicted cuts also need congestion to be starting.
