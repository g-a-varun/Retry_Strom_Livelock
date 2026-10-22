# Demo procedure for the final review (demo only, no slides)

Ma'am said: no PPT, only the demo. So the demo *is* the presentation. This file is the full procedure: what to set up, what to type, what to say at each step, and how to explain where every number on the screen comes from in our code.

The 20-slide deck (`Review3_Final.pptx`) follows exactly this order, so if she wants to see slides, or the live demo breaks, the screenshots are on slides 6 (run 1), 10 (detection), 12 (prediction), 14 (confusion matrix and accuracy), 16 and 17 (run 2). Slides 4, 5 and 13 answer ma'am's points directly: what we used (methods, not library names), how the program produces each output, and how accuracy is calculated.

`PROJECT_EXPLAINED.md` teaches the project in depth. `PRESENTATION_GUIDE.md` has the longer troubleshooting list and the file-by-file reasons. This file is the one to follow on the day.

---

## 0. Ma'am's four points, and how the demo covers each

| Her point | How we cover it | Where |
|---|---|---|
| **Metrics/matrix must be clear: what we used in the program, not "numpy" or a library name** | We run `show_metrics.py`. It prints the **confusion matrix** and works out every metric step by step (formula → numbers from the matrix → result) in plain Python, with no numpy or scikit-learn. We also say, in words, what technique each part of the program uses. | Step 6, Section 3 |
| **Accuracy must be above 70%** | The prediction model's accuracy is **91.8%** on runs it never saw. Balanced accuracy is **88.9%**, recall **84.7%**, F1 **75.5%**, ROC-AUC **0.95**. | Step 6, Section 4 |
| **Be clear about the code, even if AI helped: what we did and how it gives the output** | For every output on screen, we say which function computed it and how. There's also a short code tour of the 8 key functions. | Section 5, Section 6 |
| **No PPT, only the demo** | This procedure: about 15 minutes, in a fixed order, with a fallback if anything breaks. | Sections 1-2 |

On "matrix": we read it as both the **confusion matrix** and the **metrics** (what we used to measure the model). `show_metrics.py` covers both.

---

## 1. Before the day

### The night before (do all of this on the laptop you'll present from)

1. Open PowerShell in the project's `code` folder:
   ```
   cd X:\Projects\Retry_Strom_Livelock\code
   python --version
   ```
   It needs Python 3.9 or newer. If `python` opens the Microsoft Store, use `py` everywhere instead.
2. Install the libraries once: `python -m pip install -r requirements.txt`
3. Check the tests pass: `python -m unittest discover tests` should end with `Ran 50 tests` and `OK`.
4. **Rehearse the whole demo once**, using the run names `p1` and `p2` instead of `live1` and `live2`. A run name can only be used once, so keep `live1` and `live2` fresh for the real day.
5. Run `python prediction_model.py` once and check its output says **F1 0.75** and **PR-AUC 0.84** for our model. If it does, it's safe to run live. If your scikit-learn version gives slightly different numbers, skip Step 5 on the day and show the saved `results\prediction_results.txt` instead.
6. Note your timings. Your laptop isn't the machine we recorded on, so the live runs will be close to our numbers, not identical. What must hold: run 1 ends near **0 of 80** users served in the last 5 s; run 2 ends near **78 of 80**; run 2's first `CUT` comes while ρ is still below about 0.2.

### 20 minutes before

- Close everything that might use ports 5000-5003, and any old Python windows.
- Open **three terminals** side by side (Windows Terminal split panes are ideal), all in the `code` folder. Make the font big (Ctrl + scroll).
- Open **VS Code** on the `code` folder with these tabs already open, for the code tour:
  `services\order_service.py`, `common\retry_client.py`, `common\telemetry.py`, `features.py`, `labeling.py`, `prediction_model.py`, `controller.py`, `show_metrics.py`
- Keep `results\figures\fig1_metastable_timeline.png` ready to open, in case she wants a picture.

---

## 2. The demo, step by step

**Running order** (about 15 minutes in total):

| # | Step | Terminal | Time | What it shows |
|---|---|---|---|---|
| 0 | Opening, in words | - | 1 min | the problem and what we built |
| 1 | Unit tests | 2 | 20 s | the parts work on their own |
| 2 | Start the 4 services | 1 | 20 s | the real system |
| 3 | Run 1: fault, no controller | 2 | 2.5 min | the livelock happens, and doesn't end |
| 4 | Detection on our dataset | 2 | 30 s | how we decide "livelock", 0 false alarms |
| 5 | Train and test the prediction model | 2 | 30 s | it warns 8 s early |
| 6 | **Confusion matrix and accuracy** | 2 | 2 min | **91.8% accuracy, every metric worked out** |
| 7 | Run 2: same fault, controller on | 3 + 2 | 3 min | the controller prevents it |
| 8 | Full evaluation (optional) | 2 | 1 min | confidence intervals, run-time cost |
| 9 | Code tour (when she asks) | VS Code | 2-5 min | where each thing is in the code |
| 10 | Close | 1 | 30 s | summary, Ctrl+C |

Steps 4-6 sit between the two live runs on purpose: they give run 1's leftover requests time to drain, and they tell the story in order (see the problem → detect it → predict it → prevent it).

---

### Step 0. Opening (1 minute, no command)

**Say:**
> "Ma'am, our project is about retry storms in microservices. When one service gets slow, the services calling it time out and retry. The retries are extra load on the service that is already slow, so more calls fail and more retries go out. The system ends up 100% busy, but almost no user gets served. That is livelock: nothing is blocked, everything is running, but there is no progress. In the worst case it carries on even after the slow service is fixed.
>
> We built four things: a real 4-service system where this happens on its own, a formal test that detects it, a model that predicts it about 8 seconds before it starts, and a controller that cuts retries before the storm forms. I'll show it live: first the storm without our controller, then how we detect and predict it, the model's accuracy, and finally the same fault with our controller on."

If she asks "where is the OS in this": livelock (next to deadlock in the syllabus), counting semaphores as worker slots, bounded waiting for a slot, processes and threads, IPC over sockets and files, atomic file rename, and the queueing laws (Utilization Law, Little's Law).

---

### Step 1. Unit tests (Terminal 2, 20 s)

```
python -m unittest discover tests
```

**Expected:** `Ran 50 tests in 0.0xs` then `OK`. (The 3 "reading MSCallGraph" lines are from the test of the optional Alibaba trace reader, which uses a tiny made-up file.)

**Say:** "These 50 tests check each part on its own: the window maths, the four livelock conditions, the controller's cut and restore rules, the retry loop and backoff, the retry budget, and the evaluation formulas."

---

### Step 2. Start the services (Terminal 1, 20 s)

```
python run_all.py
```

**Expected:**
```
starting payment_service on port 5001
starting inventory_service on port 5002
starting order_service on port 5003
starting gateway on port 5000
...
gateway is up
all services started. press Ctrl+C to stop.
```

**Say:** "Four separate OS processes talking over real HTTP. The user calls the gateway, the gateway calls order, and order calls payment and inventory. Payment and inventory have only 2 worker slots each, order has 8. Each service's slots are a counting semaphore. Every caller retries up to 3 times, with a 2-second timeout and backoff with jitter."

**How it works:** `run_all.py` checks the ports are free, resets every config file to healthy, starts each `services\*.py` as a child process, and waits until each answers `/health`.

---

### Step 3. Run 1: the fault with normal retries, no controller (Terminal 2, about 2.5 min)

Type as **one line**:
```
python scenario_runner.py --scenario_id live1 --mode gradual --fault latency --queue_wait 10 --normal_sec 10 --ramp_sec 10 --hold_sec 8 --recovery_sec 25
```

**Expected** (53 s plus a few seconds at the end):
```
scenario live1: gradual latency fault on payment_service, 20 users/sec (poisson), order queue wait 10.0s, 53 sec total
  [  0.0s] normal: fault removed / not active
  [ 10.0s] ramp: payment_service -> {'min_latency_ms': 164, 'max_latency_ms': 252}
  ...
  [ 20.0s] fault: payment_service -> {'min_latency_ms': 700, 'max_latency_ms': 900}
  [ 28.0s] recovery: fault removed / not active
  traffic finished, waiting for the last requests to come back...
done: 1061 user requests sent. Logs are in the data/ folder.
```

**Say while it runs:**
- At the start: "20 users per second arrive at random times, and they keep arriving even if the system is slow, like real users. The only thing this script changes is payment's latency. It never touches the retries or the queue."
- During the ramp: "Payment is getting slower in five steps, up to about 800 ms. With 2 slots it can now finish only about 2.5 requests a second. Order holds its slot while it waits for payment, so order's 8 slots fill up, requests start queueing, and the gateway's 2-second timeouts start firing. Every timeout becomes a retry."
- At 28 s: "The fault is removed now. Payment is back to 30-90 ms. Watch whether the system comes back."

Then:
```
python analyze_run.py live1
```
(10-20 s; it reads the full log files.) **Expected**, your numbers will be close:
```
phase         users  success%  goodput/s  avg latency    calls  retry%  calls/succ  orphaned%
normal          191    100.0%       19.1        207ms      594      4%         3.1       0.0%
ramp            196      9.2%        1.8       5764ms      551     46%        30.6      66.7%
fault           148      0.0%        0.0       6235ms      525     63%       525.0     100.0%
recovery        526      1.3%        0.3       6172ms     3112     37%       444.6      98.9%

users in the LAST 5 seconds of the run who got a success: 0% (0 of 80)
```

**Point at, in this order, and say:**
1. **recovery row, success% ≈ 1%:** "The fault is gone and every service is healthy, but almost nobody is served."
2. **calls ≈ 3,000 in recovery**, against about 600 when healthy, and **calls/succ ≈ 440**: "It's working five times harder than when healthy, for nothing."
3. **orphaned% ≈ 99%:** "Almost all of order's work is for requests the gateway had already given up on. We call that orphaned work."
4. **LAST 5 seconds ≈ 0 of 80:** "And it never came back. This is a self-sustaining livelock."

**Why it never comes back (say this, it's the key idea):** "After the fault, order can finish about 32 requests a second. But with 3 attempts per user, up to 3 × 20 = 60 a second arrive. 60 is more than 32, so the queue never drains. If retries were cut to 1, 20 is less than 32, and it would drain."

**How each column is produced** (`analyze_run.py`, plain loops over the log files for this run):

| Column | Computed from | How |
|---|---|---|
| users, success% | `data\user_requests.jsonl` | users whose start time falls in the phase; success = got HTTP 200 |
| goodput/s | same | successful users ÷ phase length in seconds |
| avg latency | same | average of each user's total time |
| calls, retry% | `data\telemetry_*.jsonl` (one line per call **attempt**, written by the caller) | count attempts that started in the phase; retry% = attempts with attempt_number > 1 |
| calls/succ | both | calls ÷ successful users |
| orphaned% | `data\server_order_service.jsonl` (one line per request received) | share of order's served requests whose `orphaned` flag is true |
| LAST 5 seconds | user log | users who arrived in the last 5 s of the run, and how many succeeded |

**Where the orphaned flag comes from:** every caller sends its deadline (send time + 2 s) in an `X-Deadline` header. When order finishes a request, `log_server_request()` in `common\telemetry.py` writes `orphaned = done_time > deadline`: it finished after its caller had stopped waiting.

---

### Step 4. Detection: how we decide "this is livelock" (Terminal 2, 30 s)

```
python labeling.py
```

**Expected** (end of the output):
```
scenarios: 28   windows (edge x 2s): 1878   labelled livelock: 131
...
  gateway->order_service                     0 / 212        8 / 93      46 / 104      63 / 209
...
false alarms during healthy traffic (normal phase): 0 of 636 windows
users who arrived in a window flagged livelock on any edge got a success 9.0% of the time
users who arrived in any other fault/recovery window got a success 68.1% of the time
```

This re-labels our recorded dataset (28 runs) from the raw logs. It always gives the same result, so running it live doesn't change anything.

**Say:** "To say 'this is livelock', we cut each call edge into 2-second windows and compute, from the attempt logs: ρ, retries divided by fresh calls; useful throughput; calls in flight; and the success fraction. A window is livelock only if four conditions hold, for two windows in a row:
- **C1**: more retries than fresh calls (ρ > 1);
- **C2**: useful work below half of this edge's healthy level;
- **C3**: calls in flight more than double healthy, and not falling;
- **C4**: retries not falling and success not improving compared with 4 seconds ago.

C1 is what separates livelock from plain overload: an overloaded system without retries fails C1."

**Point at:**
- **0 false alarms in 636 healthy windows**, including two control runs at 26 and 28 users/s with no fault: "It doesn't fire on a system that's merely busy."
- **9% vs 68%:** "Users who arrived in a window we flagged got a success 9% of the time, against 68% in other bad windows. So the label matches what users actually experienced."
- Almost all livelock is on **gateway→order**: "The storm forms where the retries pile up, not where the fault is."

**How it's produced:** `labeling.py`, `label_scenario()` → `features.py`, `window_stats_for_edge()` (counts per window: fresh calls, retries, successes, time in flight) → `labeling.py`, `check_conditions()` (the four tests). The healthy level of each edge comes from `edge_baseline()`: the median of its first 8 seconds in the same run.

---

### Step 5. Train and test the prediction model (Terminal 2, 30 s)

```
python prediction_model.py
```

**Expected** (the key lines):
```
examples: 1458 (edge x 2s window) from 28 scenarios, 216 positive (14.8%)
evaluation: 5-fold cross-validation grouped by scenario (test runs never seen in training)
  logistic regression (ours)                    0.68    0.85   0.75    0.84 ...
  rule: storm right now (reactive)              0.72    0.52   0.60     - ...
EARLY WARNING -- 30 livelock episodes in the data
  our model                warned before onset in 30/30 episodes, median warning 8.0s ahead (range 2-10s)
  reactive rule            warned before onset in 24/30 episodes, median warning 2.0s ahead (range 2-10s)
self-check: hand-computed risk vs scikit-learn, largest difference = 3.33e-16 (OK)
```
(If you decided in rehearsal not to run it live, open `results\prediction_results.txt` instead.)

**Say:** "Every 2 seconds, for every edge, the model answers: will this edge be in livelock at some point in the next 2 to 12 seconds? It uses 16 numbers per window:
- the edge's own state compared with its healthy level;
- how that state changed since the last window;
- the worst values of the edges below it and the values of the edge above it. That's one round of message passing over the service graph.

The model is logistic regression: a weighted sum of the 16 features, squashed into 0 to 1 by the sigmoid. That's the risk.

We test it fairly. The 28 runs are split into 5 groups, and each run is scored by a model that was trained without that run. It warned before all 30 livelock episodes, a median of 8 seconds early. The simple rule warned before only 24, 2 seconds early."

**Point at:** `30/30 ... 8.0s ahead` vs `24/30 ... 2.0s`, and the `self-check` line: "The controller computes the risk with our own short formula (weighted sum + sigmoid), not the library. This checks our formula gives exactly the same numbers as scikit-learn (difference 3 × 10⁻¹⁶, which is just rounding)."

**How it's produced:**
- `build_examples()` turns each edge-window into the 16 numbers (`features.py`, `make_features()`) and the answer (livelock in any of the next 5 windows?).
- `cross_validate()` does the grouped 5-fold test.
- `lead_times()` measures how early the first alarm came before each livelock episode.
- The final weights are saved to `results\model.json`.

---

### Step 6. The confusion matrix and the accuracy (Terminal 2, 2 min). This is ma'am's main point.

```
python show_metrics.py
```

**Expected** (exact, since it reads the saved results):
```
1. CONFUSION MATRIX (counting the 1458 windows into 4 boxes)

                               model said NO     model said YES
   really NO   (1242 windows)    TN = 1156          FP =   86
   really YES  ( 216 windows)    FN =   33          TP =  183

2. METRICS (formula  ->  numbers from the matrix  ->  result)

   Accuracy          = (TP + TN) / all       = (183 + 1156) / 1458  =  91.8 %
   Precision         = TP / (TP + FP)        = 183 / 269           =  68.0 %
   Recall            = TP / (TP + FN)        = 183 / 216           =  84.7 %
   Specificity       = TN / (TN + FP)        = 1156 / 1242         =  93.1 %
   Balanced accuracy = (Recall + Spec.) / 2  = (0.847 + 0.931) / 2 =  88.9 %
   F1 score          = 2PR / (P + R)         = 2 x 0.680 x 0.847 / 1.528 =  75.5 %
   MCC               = (TP.TN - FP.FN) / sqrt(...)  = 0.713

3. RANKING QUALITY (no threshold needed)
   ROC-AUC = ... = 0.950   (268272 pairs checked)
   PR-AUC  = ... = 0.843   (random guess: 0.148)

4. WHY WE DO NOT LOOK AT ACCURACY ALONE
                                         accuracy   bal.acc   recall       F1     MCC
   our model                               91.8 %    88.9 %   84.7 %   75.5 %    0.71
   simple rule (rho > 1 and overload)      89.8 %    74.3 %   52.3 %   60.4 %    0.56
   always say NO                           85.2 %    50.0 %    0.0 %    0.0 %    0.00
```

**Say, slowly, pointing at each part:**
1. **The matrix:** "We have 1,458 windows. For each one we know the truth, whether livelock really came in the next 2-12 seconds, and what the model said. We count them into four boxes. 183 times livelock was coming and we warned: true positives. 33 times we missed it: false negatives. 1,156 times nothing was coming and we stayed quiet: true negatives. 86 times we warned when nothing came: false positives."
2. **Accuracy:** "Accuracy is the share we got right: (183 + 1,156) out of 1,458, which is **91.8%**."
3. **The others:** "Recall is how many of the coming livelocks we caught, 183 out of 216, which is 84.7%. Specificity is how many quiet windows stayed quiet, 93.1%."
4. **Why not accuracy alone:** "Only 15% of windows are 'yes', so a model that always says 'no' is already 85% accurate, and it's useless. That's why we also report balanced accuracy, **88.9%**, which averages recall and specificity, and MCC, **0.71**, which uses all four boxes, where 0 means guessing and 1 means perfect. On both, our model is far above the simple rule and the always-no model."
5. **Ranking:** "ROC-AUC 0.95 means: take any window where livelock came and any window where it didn't. 95% of the time, our model gave the first one the higher risk. The program checks all 268,272 such pairs."
6. **No library:** "All of this is computed in `show_metrics.py` with plain loops and arithmetic, even the square root. No numpy, no scikit-learn. We cross-checked it against scikit-learn and it gives the same numbers."

**If she says "precision is only 68%, below 70":**
"Precision is the share of our warnings that were right. We chose to favour **recall**, catching the storms, because the two mistakes don't cost the same. A missed storm can leave the system stuck for good. An unneeded warning costs at most one early cut, which the controller gives back within a few seconds. Our F1, which balances the two, is 75.5%, and the decision threshold 0.5 was fixed before testing. At a threshold of 0.6, precision would be 72%, but picking the threshold after seeing the test results would be cheating."

**How it's produced:** `show_metrics.py` reads `results\prediction_oof.csv`. That file has one row per window with the true answer and the risk from a model that never saw that window's run, written by `prediction_model.py`. Then:
- `count_cells()` loops over the rows and counts TP, FP, TN and FN, using "yes if risk ≥ 0.5";
- `metrics_from_cells()` applies the formulas, and `square_root()` does Newton's method;
- `roc_auc_by_hand()` compares every (yes, no) pair;
- `pr_auc_by_hand()` walks down the windows from highest risk to lowest, adding (increase in recall) × (precision).

---

### Step 7. Run 2: the same fault with our controller on (Terminals 3 and 2, about 3 min)

Terminal 3 **first**:
```
python controller.py --mode predictive --duration_sec 90
```
Terminal 2, **straight after** (within a few seconds):
```
python scenario_runner.py --scenario_id live2 --mode gradual --fault latency --queue_wait 10 --normal_sec 10 --ramp_sec 10 --hold_sec 8 --recovery_sec 25
```
If Terminal 2 says *waiting for the previous run's leftover requests to finish...*, that's normal: it starts by itself within about 20 s.

**Terminal 3 prints one line per second**, for example:
```
t=+  12s | gw->order rho 0.07 risk 0.43 -> 3 | order->pay rho 0.12 risk 0.03 -> 3 | ...
t=+  13s | gw->order rho 0.07 risk 0.64 -> 2 CUT | ...
t=+  15s | gw->order rho 0.00 risk 0.99 -> 1 CUT | ...
t=+  21s | ... | order->pay rho 1.40 risk 0.04 -> 2 CUT | ...
t=+  33s | ... | order->pay rho 0.00 risk 0.00 -> 3 RESTORE | ...
```

**How to read a line:** for each edge, `rho` (retries ÷ fresh calls in the last 2 s), `risk` (the model's output), `-> 3` (attempts now allowed), and `CUT` or `RESTORE` when it changes. `t` counts from when the controller started, so the ramp begins at about t = 10 s plus however long you took to start Terminal 2.

**Point at the first `CUT` on `gw->order`, and say:** "Here the risk crossed 0.5 and payment's response time had doubled, so it cut gateway-to-order from 3 attempts to 2, **while rho is still 0.07**. There's no storm yet; a rule that waits for rho above 1 couldn't have fired. That's the pre-emptive cut. Two seconds later it cuts to 1. Later, order-to-payment gets its own storm, rho 1.4, and the reactive safety net cuts that too, then gives it back once the overload clears. It cuts fast and gives back slowly, like TCP congestion control."

Then:
```
python analyze_run.py live2
```
**Expected:**
```
recovery        526     23.4%        4.9       1713ms     2002      4%        16.3      82.4%

users in the LAST 5 seconds of the run who got a success: 98% (78 of 80)
```

**Point at and say:**
1. **LAST 5 seconds ≈ 78 of 80**, against 0 of 80 in run 1: "Same fault, same load, and this time it recovered by itself."
2. **calls/succ ≈ 16**, against about 440.
3. **retry% ≈ 4%** in recovery, against 37%.
4. Why the recovery row says only about 23%: "That row is the average over all 25 seconds, including the 15 or so spent draining the queue. The last-5-seconds line shows where it ended up."

When the controller's time is up, Terminal 3 prints `controller stopped, original retry policy restored.`

**How it's produced** (`controller.py`, every second):
1. `read_new_call_records()` reads only the new lines of the call logs since the last check, using byte offsets.
2. `live_window_stats()` computes the last 2 s of stats for each edge, with the same code as training.
3. `make_features()` builds the 16 features, and `risk_probability()` gives the risk: weighted sum + sigmoid, done in our own code.
4. `control_step()` decides:
   - **risky** = (risk ≥ 0.5 **and** congestion starting) **or** (ρ > 1 **and** overload > 20%);
   - if risky: `cut()`, which is attempts − 1 and backoff × 2, and the edge above is cut too;
   - if calm for 3 checks and 4 s since the last cut: `restore()`, one step back.
5. It writes `data\retry_policy.json` (write to a temp file, then rename, which is atomic). Every service reads that file on its next call, so the change takes effect immediately, with no restart.

---

### Step 8 (optional, 1 min). The full evaluation

```
python evaluate_model.py
```
(About 45 s, no services needed.) Point at:
- the **bootstrap** lines: "We resampled the 28 runs 1,000 times. The gain over the simple rule is +0.23 to +0.42, and the gain from the graph features is +0.02 to +0.19. Both intervals stay above zero, so the gains are real, not luck."
- the **run-time cost**: "One prediction takes about 5 microseconds and a whole controller round about 30. The random forest would take about 13 milliseconds per prediction, about 2,500 times slower. That's why we use logistic regression."

---

### Step 9. Code tour (when she asks to see the code)

Have these open in VS Code. About 30 seconds each; say what it does in one or two sentences.

| # | File : line | What to show | What to say |
|---|---|---|---|
| 1 | `services\order_service.py` : 51 | `worker_slots.acquire(timeout=config["queue_wait_sec"])` (the semaphore is made at line 103) | "The worker slots are a counting semaphore. A request waits at most `queue_wait_sec` for a slot, else it gets 503 busy. The threads blocked here *are* the queue." |
| 2 | `common\retry_client.py` : 52 (and 44) | the `while True` loop in `call_with_retry()`, and `backoff_with_jitter_ms()` | "Each attempt sends a deadline header, waits up to 2 s, logs one line, and if it failed and attempts are left, sleeps a backoff with jitter and tries again." |
| 3 | `common\telemetry.py` : 73-78 | `orphaned = done_time > deadline` | "This is how we measure wasted work: the service finished after its caller had given up." |
| 4 | `features.py` : 53 | `finish_window()` | "From the attempts that ended in a 2 s window: fresh calls, retries, ρ, useful throughput, calls in flight, overload." |
| 5 | `labeling.py` : 105 | `check_conditions()` | "The four livelock conditions, C1 to C4, exactly as we defined them." |
| 6 | `features.py` : 231 and 288 | `make_features()` (the `down_` and `up_` features) and `risk_probability()` | "The graph features take the worst values from the edges below and the values of the edge above. The risk is our own weighted sum and sigmoid." |
| 7 | `prediction_model.py` : 163 | `cross_validate()` with `GroupKFold` | "Each run's windows stay together, so a test run is never seen in training." |
| 8 | `controller.py` : 152 (and 95, 127, 140) | `control_step()`, `congestion_starting()`, `cut()`, `restore()` | "Risky → cut one attempt and double the backoff, and cut the edge above too. Calm for 3 checks → give one step back." |
| 9 | `show_metrics.py` : 39 and 75 | `count_cells()`, `metrics_from_cells()` | "The confusion matrix is four counters in a loop; every metric is a formula on those four numbers." |

---

### Step 10. Close (30 s)

**Say:** "To sum up: with normal retries, a 20-second slowdown turned into a failure that never ended, 0 of 80 users served at the end. Our detector found these livelocks with zero false alarms. Our model predicts them about 8 seconds ahead with 91.8% accuracy on runs it never saw. With our controller on, the same fault recovered by itself, 78 of 80 users served at the end. The lesson: retries help with short, fast failures and hurt when a service is saturated, so the retry budget has to adapt while the system runs. Thank you, ma'am."

Then **Ctrl+C in Terminal 1** to stop the services.

---

## 3. What we used in the program, in our own words (not library names)

If she asks "what did you use?", answer with the **technique**, then say which part is our code. Never just "we used scikit-learn".

| Part | The technique, in words | Our own code does | A library only does |
|---|---|---|---|
| The 4 services | small web servers, one OS process each, one thread per request | slots (counting semaphore with a timed wait), simulated work, errors, logging, calling the next service | **Flask** receives HTTP requests and hands each to a thread |
| Calls between services | HTTP request with a timeout | the retry loop, exponential backoff with equal jitter, deadline header, retry budget (token bucket) | **requests** sends the HTTP call and enforces the timeout |
| Fault injection | change a running service's latency or error rate through a config file it reads on every request | all of it | - |
| Logs | JSON Lines: one record per attempt, per received request, per user | all of it, including atomic file replace and the incremental "tail" reader | - |
| Window statistics | counting per 2 s window: fresh calls, retries, ρ, throughput, time-averaged calls in flight | all of it (`features.py`) | - |
| Livelock detection | four formal conditions, 2 windows in a row, each edge compared with its own healthy median | all of it (`labeling.py`) | - |
| Graph features | one round of message passing: worst values from the edges below, values of the edge above | all of it (`features.py`) | **networkx** only stores snapshots so they can be drawn |
| Prediction model | **logistic regression**: risk = 1 / (1 + e^−(w·x + b)), features standardised (x − mean) / std, classes weighted equally | computing the risk at run time (`risk_probability()`), building examples and labels, lead time | **scikit-learn** finds the 16 weights + bias during training (it minimises the weighted log loss with an iterative optimiser called L-BFGS); computes mean and std; splits runs into 5 groups |
| Testing the model | grouped 5-fold cross-validation, out-of-fold scores | building groups by run, baselines, ablations, lead time | scikit-learn's `GroupKFold` makes the split |
| Metrics | confusion matrix; accuracy, precision, recall, specificity, balanced accuracy, F1, F2, MCC, kappa; ROC-AUC, PR-AUC; Brier score and calibration error; bootstrap 95% intervals | **all of them**, by hand (`show_metrics.py`, `evaluate_model.py`) | scikit-learn is used only to **cross-check** our formulas, and for ROC/PR-AUC inside `evaluate_model.py` (`show_metrics.py` computes those by hand too) |
| Controller | AIMD feedback control (as in TCP): cut fast on congestion, restore slowly | all of it (`controller.py`) | - |
| Experiment statistics | mean ± standard deviation; Welch's t-test | means, standard deviations, recovery time, orphaned % | **scipy** computes the t-test p-value |
| Figures | line and bar charts | choosing what to plot | **matplotlib** draws them |

**numpy:** our code never imports numpy. scikit-learn uses it internally.

**If she asks how training (`fit`) works:** "We start with weights at zero. For every training window we compute the risk and measure how wrong it is with the log loss: if livelock came and we said 0.1, the loss is −log(0.1), which is large. The optimiser works out in which direction each weight should move to reduce the total loss, moves it, and repeats until the loss stops going down. Rare 'yes' windows count about 3.4 times and 'no' windows about 0.6 times, so both classes matter equally. A small penalty keeps the weights from growing too big. The result is 16 weights and a bias, saved in `model.json`, and from then on our own code uses them."

---

## 4. The accuracy, in one place

| Metric (prediction model, runs it never saw) | Value |
|---|---|
| **Accuracy** | **91.8%** (1,339 of 1,458 windows right) |
| Balanced accuracy | 88.9% |
| Recall (storms caught) | 84.7% |
| Specificity (quiet windows kept quiet) | 93.1% |
| F1 | 75.5% |
| Precision | 68.0% (see the answer in Step 6) |
| MCC | 0.71 (95% interval 0.61-0.79) |
| ROC-AUC | 0.95 |
| PR-AUC | 0.84 (random guess 0.15) |
| Early warning | before 30 of 30 episodes, median 8 s |

**"Accuracy of what?"**
- **The prediction model:** 91.8% on 1,458 windows from runs it never saw.
- **The detector** has no accuracy figure, because there is no outside "true livelock" label anywhere: the detector *defines* the label. We check it in other ways: 0 false alarms in 636 healthy windows, 9% vs 68% user success, and a threshold sensitivity sweep.
- **The controller** is measured by what users got: in 5 repeated runs, 0 of 5 recovered with normal retries against 5 of 5 with our controller.

---

## 5. How the outputs come out, end to end (one-minute version)

If she asks "how does it actually produce this output?", walk through the chain:

1. **Logs.** Every call attempt, every request a service receives, and every user request is written as one JSON line (`common\telemetry.py`).
2. **Windows.** For each call edge, the logs are cut into 2-second windows and counted: fresh calls, retries, successes, time in flight (`features.py`).
3. **Label.** The four conditions decide whether each window is livelock (`labeling.py` → `data\labeled_windows.csv`).
4. **Features.** Each window becomes 16 numbers (`features.py`, `make_features()`).
5. **Model.** Logistic regression is trained with grouped cross-validation. Its weights go to `results\model.json`, and its out-of-fold risks to `results\prediction_oof.csv` (`prediction_model.py`).
6. **Metrics.** The confusion matrix and every metric are computed from those risks (`show_metrics.py`, `evaluate_model.py`).
7. **Controller.** Live, every second, the same features and the same weights give a risk. The controller cuts or restores retries by rewriting `data\retry_policy.json` (`controller.py`).
8. **Effect.** Every retry client reads that file on its next call (`common\retry_policy.py`), so the system's behaviour changes, and `analyze_run.py` shows the result.

---

## 6. About the code being AI-assisted

Ma'am said that even if AI helped write the code, we must be clear about what we did and how it produces the output. Our honest position: **we used AI tools as assistants while writing and cleaning up the code; the design, the experiments and the choices are ours, and we understand every part.** Don't deny using AI if asked directly. Show understanding instead:
- Be able to do the 1-minute chain in Section 5 without notes.
- Be able to open any of the 9 code-tour stops and explain it in your own words.
- Know the 3 numbers that explain the storm: capacity about **32/s**, up to **60/s** arriving with 3 attempts, and **20/s** with 1 attempt.
- Know the 4 boxes of the confusion matrix and the accuracy formula.
- Know why each design choice was made (`PROJECT_EXPLAINED.md`, Section 2.7, and Chapters 5-9).

---

## 7. If something goes wrong

| What you see | What to do |
|---|---|
| `ERROR: these ports are already in use` | close old terminals, or in PowerShell: `netstat -ano \| findstr :500`, then `taskkill /PID <number> /F` |
| `error: the gateway is not answering` | Terminal 1 isn't running: `python run_all.py` |
| `error: scenario id 'live1' was already used` | use `live3`, `live4`, ... |
| `waiting for the previous run's leftover requests...` | normal, wait about 20 s |
| `ModuleNotFoundError` | `python -m pip install -r requirements.txt` |
| `error: predictive mode needs a trained model` | use `--mode reactive` |
| Run 1 recovers (last 5 s well above 0%) | check `--queue_wait 10`; re-run with a new name and `--rps 22` |
| Run 2 doesn't reach about 100% at the end | re-run with new names, `--recovery_sec 35` and controller `--duration_sec 100` |
| Anything else | don't debug for more than 30 s. Show the matching slide (4, 8, 10, 11, 13 or 14) or the saved results instead: `type results\experiment_summary.txt` (all 50 recorded runs) and open `results\figures\fig1_metastable_timeline.png`. Say "the runs are real, so they take a minute; here are our recorded runs." |

**Saved results to fall back on** (no services needed):
- `results\experiment_summary.txt`: all 5 strategies × 2 faults × 5 runs.
- `results\prediction_results.txt`: the prediction results.
- `python show_metrics.py`: the matrix and accuracy (always works).
- `results\figures\fig1_metastable_timeline.png`: same fault, with and without control.
- `results\figures\fig6_controller_trace.png`: what the controller did.

---

## 8. Questions she's likely to ask in a demo-only review

**"What is the accuracy?"** 91.8% for the prediction model, on runs it never saw. Balanced accuracy 88.9%, recall 84.7%, F1 75.5%. (Section 4.)

**"Show me the confusion matrix."** `python show_metrics.py`, section 1. TN 1,156, FP 86, FN 33, TP 183.

**"Which algorithm did you use?"** Logistic regression on 16 hand-built features, including graph features from one round of message passing. Tested with grouped 5-fold cross-validation. Plus a formal four-condition detector and an AIMD controller.

**"Why logistic regression?"** The controller runs it every second for every edge. It's 49 numbers, takes 5 µs per prediction in our own code, and you can read its weights. A random forest scored a little higher (F1 0.80) but is about 2,500 times slower and about 1 MB.

**"Where does your data come from?"** We generated it by running the real 4-service system and injecting only the fault: 28 recorded runs for training plus 50 experiment runs. No public dataset has per-attempt retry information with fault timing. A validation script checks it six ways, and all six pass.

**"How do you know the model isn't cheating?"** Each run is scored by a model trained without that run. A control model trained on shuffled labels scores at chance (F1 0.14). Our formulas are cross-checked against scikit-learn.

**"What does the output mean?"** Use the "How it's produced" notes under each step.

**"What's new compared with existing work?"** A formal per-edge test that separates retry livelock from plain overload. Predicting it 2-12 s ahead using neighbouring edges in the call graph. A controller that acts on that prediction per edge, in steps, before the storm forms. Measuring wasted (orphaned) work directly. The closest work, RetryGuard, reacts only after rejections rise and switches retries off per service.

**"Where is the OS?"** Livelock, semaphores, bounded waiting, processes and threads, IPC, atomic rename, Utilization Law, Little's Law, AIMD feedback control.

**"Is the controller always right?"** No. An unneeded cut costs a few requests that a retry would have saved. That's why it gives retries back automatically, why the reactive safety net stays on, and why predicted cuts need congestion to be starting, a lesson from our own first version.
