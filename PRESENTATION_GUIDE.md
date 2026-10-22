# Presentation guide (for Varun and Satwik only)

This file is for the two of us, not for ma'am. It covers:

1. **The project in plain words**: what we did, why, how, and why we chose each piece.
2. **Slide-by-slide script** for `Review3_Final.pptx` (20 slides, in the same order as the demo): what to say on each.
3. **The live demo**: a pointer to `DEMO_PROCEDURE.md`, which has the full procedure.
4. **Every file in the folder** and why it exists.
5. **Cheat sheet** of the numbers to know by heart.
6. **Tough questions** with short answers. The longer question bank is `docs/VIVA_QA.md`.

The deck has no speaker notes (it goes to ma'am). What to say on each slide is in Part 2 below.

**The final review is demo only (no PPT).** Follow `DEMO_PROCEDURE.md` for that. It covers ma'am's points (metrics and the confusion matrix, accuracy, explaining the code). The 20-slide deck follows the same order as the demo, so it also works as a backup if the live demo breaks.

**To understand the project itself in depth** (how every part works inside the code, the maths, the experiments, what went wrong on the way), read `PROJECT_EXPLAINED.md` first. This file is the short version, aimed at presenting.

---

## Part 1. The project in plain words

### 1.1 The 30-second version

> When one microservice gets slow, its callers time out and retry. The retries are extra load on the service that is already slow, so more calls fail and more retries go out. The system ends up 100% busy while almost no user gets served. That is livelock. In the worst case it carries on even after the slow service is fixed.
>
> We built a real 4-service system where this happens on its own, a formal test that detects it, a model that predicts it about 8 seconds before it starts, and a controller that cuts retries before the storm forms. With static retries the system never recovered. With our controller it recovered every time.

Learn this by heart. It is the answer to "explain your project", and you will be asked that.

### 1.2 The problem from first principles

**What a retry is.** Service A calls service B over HTTP. If B does not answer within 2 s (a timeout), or answers with an error, A tries again. Nearly every real system does this, because most failures are short and a second try usually works.

**Why retries can backfire.** A retry is only free if B has spare capacity. If B is slow because it is *full*, the retry joins the same queue, makes it longer, and makes the next call even slower. Now more calls time out, so more retries go out. That is a **positive feedback loop**: the failure creates more work, and the extra work creates more failure.

**Why it multiplies.** Our gateway tries the order service up to 3 times, and every order attempt tries payment up to 3 times. So one user click can become up to 3 × 3 = **9 calls** to payment.

**Orphaned work.** The gateway gives up on an attempt after 2 s and sends a new one. But the old attempt is still sitting in order_service's queue. Later it gets a slot, does its work, and replies to nobody. That is **orphaned work**: real CPU and slot time spent on a request nobody is waiting for. It takes the slot a live request needed, so that live request also times out and becomes an orphan too.

**Why it can outlive the fault (metastable).** Just after the fault, order_service can finish about 32 requests/s (8 slots ÷ about 0.25 s each). Users still arrive at 20/s, but with 3 gateway attempts up to **60/s** arrive at order_service. 60 > 32, so the queue never drains, even though every service is healthy again. If retries are cut to 1 attempt, 20 < 32 and the backlog drains at 12/s. So the same healthy system has a good state and a stuck state, and the retry policy decides which one it ends up in. Bronson et al. call this a **metastable failure**. At least 4 of 15 major AWS outages in a decade were of this kind (Huang et al., OSDI 2022).

**Analogy, if she wants one in plain words.** A restaurant where waiters re-send an order to the kitchen if the food takes more than 2 minutes. When the kitchen slows down, it gets three copies of every order, cooks food for tables that have already left, and the tables that are still waiting get nothing. Even after the cook is back to normal speed, the pile of duplicate orders keeps the kitchen busy forever, unless someone stops the waiters re-sending.

### 1.3 Why this is an Operating Systems project

The OS slide was removed from the deck, so have this ready to say out loud. She may well ask "where is the OS in this?"

| OS concept (syllabus) | Where it is in our project |
|---|---|
| **Livelock** (next to deadlock in the process-synchronisation chapter): processes keep running and changing state, but none makes progress | The whole project. Our C1-C4 test is a formal livelock detector for call edges |
| **Receive livelock** in interrupt-driven kernels: the kernel spends all its CPU on packets it later drops (Mogul and Ramakrishnan, 1997, not in our reference list because we kept it to 2019-2026) | order_service spends its slots on requests the caller already gave up on (orphans) |
| **Counting semaphore** | Each service has `c` worker slots: `threading.Semaphore(8)` for order, `Semaphore(2)` for payment and inventory |
| **Bounded waiting** | A request waits at most `queue_wait_sec` (1 s or 10 s) for a slot. That one bound decides whether the storm can outlive the fault |
| **Processes, threads, IPC** | 4 separate OS processes talking over sockets (HTTP); each request is a thread |
| **Queueing theory / Utilization Law** U = X·S / c ≤ 1 | `validate_data.py` checks no service is ever over 100% busy and that saturation throughput matches c / S |
| **Timeouts and deadlines** | 2 s per attempt; the deadline travels in a header so the callee can tell when its work is wasted |
| **Feedback control (AIMD, as in TCP congestion control)** | The controller cuts fast and gives back slowly |
| **Atomic file replace** (rename is atomic) | The controller writes `retry_policy.json` to a temp file and renames it, so a service never reads a half-written policy |

One line to say: *"It is the classic OS livelock, just happening between services instead of between threads."*

### 1.4 What we built (the four parts plus the dataset)

```
 users (Poisson, 20/s) --> gateway --3 attempts--> order_service --3 attempts--> payment_service
                                                                 \--3 attempts--> inventory_service
         every attempt and every request is logged (JSON lines)
                    |
   offline:  2 s windows per edge --> C1-C4 labels --> 16 features --> logistic regression (model.json)
   online:   controller, every 1 s: new log lines --> same features --> risk --> cut / hold / restore
                                                                         --> writes retry_policy.json
                                                    every retry client reads it on its next call
```

1. **Testbed.** 4 real processes (Flask): gateway :5000, payment :5001, inventory :5002, order :5003. Every caller uses the same retry client (up to 3 attempts, 2 s timeout, 100 then 200 ms backoff with jitter, deadline header). Faults are injected by changing a service's latency or error rate in `configs/<service>.json`, which the service reads on every request.
2. **Detector** (`labeling.py`). Cuts the logs into 2-second windows per call edge and asks four questions (C1-C4). All four true for 2 windows in a row = livelock.
3. **Predictor** (`prediction_model.py`, `evaluate_model.py`). Every 2 s, for every edge: "will this edge be in livelock at some point in the next 2-12 s?" 16 features, logistic regression, tested on scenarios it never saw.
4. **Controller** (`controller.py`). Every second, per edge: compute the same features from the live logs, compute the risk from the saved weights, then cut, hold, or give back one retry step.
5. **Dataset.** No public dataset has per-attempt retry data, so we generate our own by running the real system and injecting only the fault (`generate_dataset.py`), then validate it (`validate_data.py`).

### 1.5 How each part works (enough to answer "how?")

**The four livelock conditions**, per edge, per 2 s window (ρ = retries ÷ fresh calls):

| | Test | Plain meaning |
|---|---|---|
| C1 | ρ > 1 | more retries than fresh calls |
| C2 | S < 0.5 · S\* | useful throughput below half of this edge's healthy level |
| C3 | N > max(2N\*, 1) and N(w) ≥ 0.8 · N(w−2) | lots of calls in flight and not going down |
| C4 | retries(w) ≥ 0.8 · retries(w−2) and P(w) ≤ P(w−2) + 0.05 | the retry loop is not dying out and success isn't improving |

S\* and N\* are the edge's own healthy levels (median over the first 8 s of the same run). Why all four: C1 + C4 separate livelock from plain overload (plain overload has few retries, so C1 fails). C2 + C3 say "no progress, work stuck inside". Two windows in a row removes one-off noise.

**The 16 features** (per edge, per window):
- *Own edge (5):* ρ, fail ratio, overload ratio (503s + timeouts), latency ratio and concurrency ratio (relative to healthy).
- *Temporal (3):* change in ρ, fail ratio and latency ratio since the last window.
- *Graph (5):* worst overload, latency ratio and fail ratio among the edges **below**; ρ and overload of the edge **above**. This is one round of message passing on the call graph.
- *Edge id (3):* one-hot, because each edge has its own normal level.

**The model.** Logistic regression, class-balanced, standardised features: risk p = 1 / (1 + e^−(w·x + b)). Saved as plain numbers in `model.json` (49 numbers), so the controller computes it by hand every second. Checked against scikit-learn: largest difference 3.3 × 10⁻¹⁶.

**The evaluation.** 5-fold cross-validation **grouped by scenario**: all windows of one run go into the same fold, so the test run is never seen in training. Every window gets an "out-of-fold" score from a model that never saw its run (`prediction_oof.csv`). `evaluate_model.py` then computes every metric from those scores.

**The controller** (every 1 s, every edge):
- Fewer than 3 calls in the last 2 s: HOLD (nothing to judge).
- *Risky* means: reactive mode, ρ > 1 and overload > 20%. Predictive mode, risk ≥ 0.5 **and** congestion starting (overload ≥ 10% on this edge or one below, or the callee's response time at least doubled), **or** the reactive rule as a safety net.
- If risky: also mark the edge above (path rule, because retries multiply down the chain). CUT = attempts − 1 (min 1), backoff × 2 (max × 4), at most once every 2 s.
- If not risky: RESTORE one step only after 3 calm checks and at least 4 s since the last cut.
- Cut fast, give back slowly = AIMD, as in TCP.
- It restores when **overload clears**, not when ρ is low. With 1 attempt, ρ is 0 by definition, which made our Review-2 controller flap on and off.

### 1.6 Why we chose each thing

| Choice | Why |
|---|---|
| Real processes and HTTP, not a simulator | The storm has to emerge on its own from real queues, threads and timeouts. We only inject the fault |
| Open-loop Poisson traffic | Real users keep arriving even when the site is slow. A closed loop (wait for the reply before sending the next) would quietly slow the load down and hide the storm |
| Payment and inventory with only 2 slots | Makes them the bottleneck, so a slowdown there fills order's queue |
| Two queue lengths (1 s and 10 s) | 1 s gives the fault-coupled kind (stale requests rejected quickly); 10 s gives the self-sustaining kind (orphans stay in the queue) |
| 5% built-in errors on healthy services | Real systems always have a trickle of failures and retries. It also shows why "no retry" is not the answer: it loses about 12% of users when nothing is wrong |
| Jitter in the backoff | Without jitter all callers retry at the same instant, which is unrealistic and makes it worse |
| 2-second windows | Long enough to have several calls per edge, short enough to react in time |
| Four conditions, not just ρ | ρ alone fires on healthy traffic with a few retries, and can't tell livelock from plain overload |
| Logistic regression, not a bigger model | Small, explainable, computed by hand every second (5 µs). A random forest is a bit better (F1 0.80 vs 0.75) but about 2,500 times slower per prediction and about 1 MB |
| Grouped cross-validation | Windows of the same run are almost copies of each other. Mixing them into train and test would leak the answer |
| Threshold 0.5 fixed in advance | Choosing the best threshold on the test data would inflate the result |
| AIMD (cut fast, restore slowly) | Proven idea from TCP: react fast to congestion, probe carefully when it's gone |
| Congestion gate on predicted cuts | Learnt the hard way (report Section IX-D, `PROJECT_EXPLAINED.md` 9.6): without it the model cut useful retries during an error burst |
| Per-edge, graded cuts (3→2→1) | Retries are useful in some places and harmful in others at the same moment. On/off per service (RetryGuard) throws away useful retries |

### 1.7 What we found (six lines)

1. Static retries turned a 20 s slowdown into a failure that never ended: 1.7% success after the fault, 0 of 5 runs recovered, 98.9% of order_service's work was orphaned.
2. The detector found 131 livelock windows, with 0 false alarms in 636 healthy windows, and users in flagged windows succeeded only 9% of the time (68% in other bad windows).
3. The model warned before 30 of 30 livelock episodes, a median of 8 s early (the reactive rule: 24 of 30, 2 s). MCC 0.71, ROC-AUC 0.95, on unseen scenarios.
4. The predictive controller: livelock formed in 0 of 5 runs, recovered in 5 of 5 (14.6 s on average), 100% success when healthy.
5. In an error burst (where retries help), it kept 70.0% success against 73.2% for static retries (not a significant difference), while the 10% retry budget dropped to 54.3%.
6. Main lesson: retries help with short, fast failures and hurt during saturation, so the retry budget has to adapt while the system runs, ideally before the storm forms.

### 1.8 Novelty in our own words

There is no novelty slide in the deck (the report has the related work and research gap), so **say this when you explain the architecture (slide 3), or whenever she asks what's new**:

> "What is new: one, a formal per-edge test that separates retry livelock from plain overload. Two, predicting it 2-12 s before it starts, using features from neighbouring edges in the call graph. Three, a controller that acts on that prediction per edge, in steps, with a congestion gate. Four, we measure the wasted orphaned work directly with a deadline header. And because no public dataset fits, a validated dataset generator. The closest work, RetryGuard, reacts after rejections rise and switches retries off per service. We act before the storm forms, per call edge, in graded steps."

### 1.9 If she asks "what changed since Review 2?"

- The controller used to flap: it restored retries when ρ dropped, but ρ is 0 by definition at 1 attempt. Now it restores only after overload clears.
- The load generator used to pause between phases, which gave the system a free break and hid the self-sustaining case. Now traffic is continuous and open-loop.
- In Review 2 we said a self-sustaining livelock was impossible. It needs orphaned work (a long queue); now we show it live and explain it with the capacity inequality.
- C4 used a strict trend test (a coin flip in a steady storm). Now it has a 20% tolerance, justified by the sensitivity sweep.
- The model was tested on a time split with only 18 positives and no baseline. Now: grouped CV, 216 positives, baselines, ablations, lead time, and a full evaluation (MCC, ROC-AUC, calibration, 95% intervals, cost).
- The controller now actually uses the prediction; a 10% retry-budget baseline was added; 5 runs per strategy with statistics.
- Safe Ctrl+C, wait-until-idle between runs, 50 unit tests, data validation.

### 1.10 If she asks "what next?" (future work)

A temporal graph neural network (TGN) over longer history, since the random forest already shows headroom; deeper call chains across several machines (Kubernetes / service mesh); dropping orphans at the queue using the deadline (LIFO / CoDel-style queues); learning the size of the cut instead of fixed steps; testing on real production data if a dataset with per-attempt retries ever becomes public.

---

## Part 2. Slide-by-slide script (20 slides)

The deck follows the demo: problem → architecture → what we used → how each output is produced → run 1 → why it can't recover → dataset → detection → prediction → how accuracy is calculated → accuracy output → controller → run 2 → results. Slides 4, 5 and 13 answer ma'am's points directly (methods not library names, how the code gives each output, the accuracy formulas). Slides 6, 10, 12, 14, 16 and 17 are screenshots of exactly what the demo prints, so the deck doubles as a backup if the live demo breaks.

**Timing.** About 12-15 minutes if you present the slides on their own, or use them as the backdrop while the demo runs (`DEMO_PROCEDURE.md` gives the order).

**Golden rules.** Say the main point of each slide first. Point at the screen. Never make up a number; if you don't know, say what you'd check. Say weak results before she does.

#### Slide 1. Title
"Good morning ma'am. Our project is *Predicting and Pre-empting Retry-Storm-Induced Livelock in Microservice Architectures*. When a service gets slow, its callers retry, the retries make it slower, and the system ends up busy but doing nothing useful. We built a real system where this happens, a test that detects it, a model that predicts it about 8 seconds early, and a controller that stops it before it forms."

#### Slide 2. The Problem
"A service gets slow, callers time out and retry. Each retry is extra load on the same slow service, so more calls fail and more retries go out: the feedback loop in the figure. Retries also multiply: 3 gateway attempts times 3 order attempts is 9 payment calls for one click. The end state is livelock: every thread running, nothing blocked, no progress. The worst case is when the storm outlives the fault, a metastable failure [1]; at least 4 of 15 major AWS outages in a decade were of this kind [2]."
If asked "where is the OS?": Part 1.3.

#### Slide 3. System Architecture
"Five layers. A: a load generator sends users at a fixed random rate, and a fault injector changes only the faulty service's latency or error rate; the manifest records exact phase times, our ground truth. B: four OS processes over real HTTP, every caller with the same retry client. C: every attempt and request is logged as one JSON line. D: offline we cut the logs into 2-second windows per edge, label them, build 16 features and train the model. E: online, every second the controller reads new log lines, computes the risk and decides cut, hold or restore, and writes retry_policy.json. The red arrow closes the loop: every retry client reads that file on its next call."
Good moment for the novelty paragraph (Part 1.8) if she hasn't asked yet.

#### Slide 4. What We Used in the Program (Methods, Not Library Names)
"Ma'am asked us to say what we used, not library names, so this table goes part by part: the technique in words, what our own code does, and the one job a library does. The services are separate OS processes, and the worker slots are a counting semaphore with a timed wait. Flask only receives the HTTP request. The retry loop, backoff with jitter, deadline and retry budget are ours; the requests library only sends one HTTP call. Detection, features, the risk formula, every metric and the controller are all our code. scikit-learn only finds the weights during training and splits the folds, and we use it to cross-check our formulas. We never import numpy."
If she asks how training works: `DEMO_PROCEDURE.md`, Section 3 ("If she asks how training (`fit`) works").

#### Slide 5. How the Program Produces Each Output
"This is the order of the demo. Each command reads the files written by the one before it. The scenario runner sends users and injects the fault, and every service writes one JSON line per attempt and per request. analyze_run reads those lines and counts per phase. labeling cuts them into 2-second windows and applies C1 to C4. prediction_model builds the 16 features and tests on runs it never saw. show_metrics counts the four boxes of the confusion matrix. The controller does the same feature and risk computation live, every second, and writes the policy file. In brackets is the function we can open to show each step."

#### Slide 6. Output: Run 1, Normal Retries, No Controller
"This is what you'll see live in demo step 3. Only payment's latency is changed. Recovery row: the fault is gone, but only 1.3% of users succeed. 3,112 internal calls, 444.6 calls per success, 98.9% of order's work orphaned. In the last 5 seconds, 0 of 80 users succeeded. It never came back."

#### Slide 7. Why the Storm Outlives the Fault
"After the fault, order can finish about 32 requests per second: 8 slots at about a quarter second each. Users arrive at 20 per second, but with 3 attempts up to 60 per second arrive. 60 is more than 32, so the backlog never drains. With 1 attempt, 20 is less than 32 and it drains at 12 per second. With a short 1-second queue, stale requests are rejected before the gateway gives up, so there are no orphans and the storm ends with the fault. With a 10-second queue, abandoned requests are processed later for nobody, and that keeps it going."
**Know this slide cold.**

#### Slide 8. Dataset: Why We Generated It, and How We Checked It
"To label retry livelock, every call needs its attempt number, its outcome and the fault timing. No public dataset has all three. The OSDI 2022 study that established this failure class generated its data the same way we do: run a real system, inject only the fault, log every attempt. We drew 24 random scenarios over 6 factors plus 4 no-fault controls, 78 runs in total with the experiments. A validation script checks six things that must hold for a real system, and all six pass. Regenerating the dataset gave 131 to 135 livelock windows each time."

#### Slide 9. Livelock Detection: Formal Definition
"Per edge and per 2-second window. Rho is retries over fresh calls. S is useful throughput, N calls in flight, P the success fraction. S-star and N-star are this edge's healthy levels from the first 8 seconds of the same run. C1: more retries than fresh calls. C2: useful work below half of healthy. C3: lots in flight and not falling. C4: retries not falling and success not improving. All four, two windows in a row. C1 is what separates livelock from plain overload."

#### Slide 10. Output: Livelock Detection on the 28 Recorded Runs
"Demo step 4. 131 livelock windows, 0 false alarms in 636 healthy windows, including two high-load runs with no fault. Users who arrived in a flagged window got a success 9% of the time, against 68% in other bad windows, so the label matches what users felt. The storm forms on gateway-to-order, where retries pile up, not on payment, where the fault is."

#### Slide 11. Livelock Prediction: Task, Features and Model
"Every 2 seconds, for every edge: will it be in livelock in the next 2 to 12 seconds? 16 features: the edge's own state, how it changed, and graph features, meaning the worst values of the edges below and the values of the edge above. The model is logistic regression, a weighted sum and a sigmoid, saved as 49 numbers that the controller computes itself every second. We test it on runs it never saw."

#### Slide 12. Output: Training and Testing the Prediction Model
"Demo step 5. On runs it never saw: F1 0.75, against 0.60 for the simple rule. It warned before all 30 livelock episodes, a median of 8 seconds ahead; the rule only before 24, 2 seconds ahead. The self-check shows our hand-written risk formula gives the same numbers as scikit-learn."

#### Slide 13. How Accuracy Is Calculated: Confusion Matrix and Formulas
"Every test window goes into one box: what the model said against what really happened. 183 storms we warned about, 1,156 quiet windows we left alone, 86 false alarms, 33 missed. Accuracy is the right answers over all windows: 183 plus 1,156 over 1,458, 91.8%. Each other metric is a formula on the same four numbers, shown with our numbers filled in. Because only 15% of windows are 'yes', we also give balanced accuracy, 88.9%, and MCC, 0.71, which don't reward always saying no."

#### Slide 14. Output: Confusion Matrix and Accuracy
"Demo step 6, ma'am's main point. We count the 1,458 windows into four boxes: 183 true positives, 33 missed, 1,156 true negatives, 86 false alarms. Accuracy is (183 + 1,156) / 1,458 = **91.8%**. Because only 15% of windows are 'yes', always saying 'no' would already be 85% accurate, so we also report balanced accuracy, 88.9%, and MCC, 0.71. Everything here is computed by show_metrics.py in plain Python."
If asked about precision 68%: `DEMO_PROCEDURE.md`, Step 6.

#### Slide 15. Controller Design (Flowchart)
"Every second, for every edge. Fewer than 3 calls: hold. Risky means: risk at least 0.5 and congestion starting below the edge, or rho above 1 with more than 20% overload as a safety net. If risky: cut one attempt and double the backoff, and cut the edge above too, at most every 2 seconds. Calm for 3 checks and 4 seconds since the last cut: give one step back. Cut fast, give back slowly, like TCP. We restore on overload clearing, not on low rho, because rho is zero by definition at one attempt."

#### Slide 16. Output: Run 2, Same Fault, Predictive Controller On (1/2)
"Demo step 7. One line per second. At 13 seconds it cuts gateway-to-order from 3 to 2 while rho is still 0.07: no storm yet, so no reactive rule could fire. At 15 seconds it goes to 1. At 21 seconds order-to-payment gets its own storm and the safety net cuts it, and it's given back at 33 seconds."

#### Slide 17. Output: Run 2, Same Fault, Predictive Controller On (2/2)
"Same fault, same load: in the last 5 seconds 78 of 80 users succeeded, against 0 of 80 without the controller. Calls per success fell from 444 to 16. The recovery row says 23% only because it averages the whole 25 seconds, including draining the backlog. When the controller stops, it puts the original policy back."

#### Slide 18. Results: Five Retry Strategies, Two Faults, 5 Runs Each
"The full experiment: 5 strategies, 2 faults, 5 runs each, 50 runs in shuffled order. No retry loses 12% of users when healthy. Static retries never recover from the metastable fault. The 10% budget throws away useful retries in the error burst. Reactive fixes the storm only after it forms. Predictive is the only one that is never bad: recovered 5 of 5, livelock never formed, and 70% in the error burst against 73% for static retries, which is not a significant difference."

#### Slide 19. Conclusion
"We built the testbed, reproduced the self-sustaining livelock live, detected it formally, predicted it about 8 seconds ahead with 91.8% accuracy on unseen runs, and prevented it with a controller that changes retries per edge. Main lesson: retries help with short, fast failures and hurt during saturation, so the retry budget has to adapt while the system runs, ideally before the storm forms. Thank you, ma'am."

#### Slide 20. References
Don't read them. "These are the works cited on these slides, in IEEE format. The full list of 21, with related work and the research gap, is in the report."

**What's no longer in the deck, but in the report** (have it ready if she asks): related work and the research gap (report Section III), the testbed parameters and the orphan sequence diagram (Section IV), the threshold sensitivity sweep (Section VII), the full model evaluation with confidence intervals and cost (Section VIII), the congestion-gate lesson (Section IX), the full experiment tables (Section XI), and the limitations (Section XIII).

---

## Part 3. The live demo

The full procedure for tomorrow (setup, the order, exact commands, what to say, where each number comes from in the code, what to do if it breaks) is in **`DEMO_PROCEDURE.md`**. Follow that file on the day.

---

## Part 4. Every file in the folder and why it exists

Folder: `X:\Projects\Retry_Strom_Livelock`.

### 4.1 Top level

| File / folder | Why it exists |
|---|---|
| `Review3_Final.pptx` | The deck: 20 slides in the demo's order (what we used, how each output is produced, the outputs, the accuracy calculation), no speaker notes |
| `Review3_Final.pdf` | The same deck as PDF, in case the room's PC has no PowerPoint or fonts move |
| `PRESENTATION_GUIDE.md` | This file. For us only; don't hand it in |
| `PROJECT_EXPLAINED.md` | The whole project explained from scratch, chapter by chapter, with a self-test. For us only; don't hand it in |
| `DEMO_PROCEDURE.md` | The step-by-step procedure for the demo-only final review. For us only |
| `Report_IEEE.pdf`, `Report_and_Slides.pdf`, `report\` | The IEEE-format report (16 pages, with results and documentation); the single submission file (report pages 1-16, then the 20 slides, with a bookmark for each); and the report's LaTeX source with its figures (`figs\plot_*.pdf` from `code\report_plots.py`, `figs\tikz_*.tex` diagrams drawn in LaTeX) |
| `DELETE\` | The old 57-slide deck (`Review3_Full_57_slides.pptx` / `.pdf`), kept only in case you want something back from it. Ignored by git; safe to delete |
| `README.md` | One-page map of the folder |
| `code\` | The whole project: testbed, detection, prediction, controller, experiments, results |
| `docs\` | Our study material: viva question bank, code walkthrough, dataset search |
| `figures\` | Three architecture diagrams from **Review 2** (`Fig1_system_architecture.jpg`, `Fig2_testbed_architecture.jpg`, `Fig_pipeline.jpg`). The Review-3 deck draws its own diagrams, so nothing uses these any more. Kept as history; safe to delete |
| `.gitignore` | Tells git which files not to track: Python caches, files the running system rewrites (`retry_policy.json`, temp files, `graph_snapshots.pkl`, pipeline logs), the pipeline's backups, big trace downloads, Office/Windows clutter |
| `.git\` (hidden) | Git's version history of the project (one commit, about 34 MB). Only needed if you use git or GitHub. If you hand in a zip, you can leave it out |

### 4.2 `code\`: the scripts you run

| File | Why it exists |
|---|---|
| `README.md` | Explains the code: modules, how to run, what each output means |
| `QUICKSTART.md` | Only the demo commands, in order (Part 3 is the detailed version) |
| `requirements.txt` | The 6 libraries to install: flask, requests, scikit-learn, scipy, networkx, matplotlib |
| `run_all.py` | Starts the four services as separate processes, checks the ports are free, waits until each answers, and resets every config to healthy. Also has `wait_until_idle()`, used so one run's leftover work can't leak into the next |
| `scenario_runner.py` | Runs **one** experiment: sends open-loop Poisson users to the gateway and walks through the phases (normal → ramp → fault → recovery), changing only the faulty service's config. Writes the run's manifest (exact phase times) to `data\scenarios.jsonl`. Refuses a re-used name; on Ctrl+C removes the fault and marks the run "interrupted" |
| `analyze_run.py` | The per-phase report for one run (users, success%, calls, retry%, calls/success, orphaned%) and the "LAST 5 seconds" line. Used in the demo |
| `controller.py` | Module 7, the retry-budget controller. Every second: read new log lines, compute per-edge stats and the model's risk, then CUT / HOLD / RESTORE, and write `data\retry_policy.json`. Modes `reactive` and `predictive`. Puts the original policy back when it stops |
| `features.py` | All the maths that turns call records into numbers: 2-second windows, ρ, throughput, in-flight count, the 16 features, the reactive alarm, and the risk formula. **One copy** used offline, in training and live, so the model is trained on exactly what the controller computes |
| `labeling.py` | Module 4, the detector. Applies C1-C4 to every edge and window, writes `labeled_windows.csv` and `baselines.json`, prints the detection report; `--sensitivity` runs the threshold sweep |
| `graph_builder.py` | Module 5. Builds the time-varying service graph (one snapshot per window, edges coloured by risk) and draws `graph_snapshots.png` |
| `prediction_model.py` | Module 6, training. Builds examples, runs grouped 5-fold CV, baselines, ablations, random forest, shuffled control, lead time; writes `model.json`, `prediction_oof.csv`, `prediction_results.txt` and `prediction_metrics.json` |
| `show_metrics.py` | Prints the confusion matrix and every metric (accuracy, precision, recall, F1, MCC, ROC-AUC, PR-AUC) worked out step by step in plain Python, no libraries. Made for the demo: ma'am asked for the metrics to be clear |
| `evaluate_model.py` | Module 6, full evaluation of the out-of-fold scores: confusion matrix, accuracy, balanced accuracy, precision, recall, specificity, F1/F2, MCC, kappa, ROC-AUC, PR-AUC, Brier, ECE, bootstrap 95% intervals, threshold sweep, per-group results, run-time cost, shuffled control ×20. Writes `model_evaluation.txt/.json` and `fig5c_model_evaluation.png` |
| `generate_dataset.py` | Makes the dataset: 24 random scenarios drawn from 6 factors with seed 7, plus 4 no-fault control runs (two at high load), each run through `scenario_runner.py` |
| `experiment_arms.py` | Runs the experiments: 5 strategies × 2 faults × 5 repetitions in shuffled order, starting the controller for the two controller strategies; writes `experiment_runs.csv` and `experiment_summary.txt` (mean ± std, Welch t-tests) |
| `validate_data.py` | Checks the data can be trusted, in 6 groups: bookkeeping, retry rules, queueing theory, fault injection, load generator, timestamps. Writes `data_validation.txt` |
| `make_figures.py` | Draws every figure in `results\figures\` and writes `results\RESULTS.md` |
| `report_plots.py` | Draws the report's plots (`report\figs\plot_*.pdf`) from the same data, in IEEE print style: Times font, column width, no titles inside the plot |
| `run_pipeline.py` | One command for everything: runs the 9 steps in order, starting and stopping its own services, backing up the previous results first. `--quick` for a smaller run |
| `calibrate_from_trace.py` | **Optional**, not used for any result. Reads a file from the public Alibaba 2021 trace to compare call-graph shape with ours. Exists because we tried a real dataset first (it has no retry or status fields, see slide 8). A unit test runs it on a tiny made-up file, which is where the "reading MSCallGraph" lines come from |

### 4.3 `code\common\`: shared building blocks

| File | Why it exists |
|---|---|
| `config.py` | Every setting in one place: ports and URLs, the call topology, slot counts, default retry policy (3 attempts, 2 s timeout, 100 ms backoff), user timeout 15 s, file paths |
| `fault_injection.py` | Inside each service: reads that service's config file **on every request** and simulates the work (sleeps for a random time in the latency range, or fails with the given error rate). That's how a scenario changes a running service without restarting it |
| `retry_client.py` | The retry loop every caller uses: attempt → log → back off with equal jitter → try again, up to the policy's limit. Sends the deadline header and a call id; optional token-bucket retry budget |
| `retry_policy.py` | Reads and writes `data\retry_policy.json` (attempts and backoff per edge). If the file is half-written at that instant, it keeps the last good policy |
| `safe_files.py` | Atomic file writes (write a temp file, then rename), with a retry for Windows, where a file being read can't be replaced for a moment |
| `service_helpers.py` | Small shared bits for the services: counts requests in flight, reads the deadline header, silences Flask's per-request logging |
| `telemetry.py` | Writes the JSON-line logs, and reads them back. Has the "tail" reader the controller uses: remembers where it stopped in each file and reads only new lines |

### 4.4 `code\services\`: the four microservices

| File | Why it exists |
|---|---|
| `gateway.py` | Port 5000, the entry point users call (`/checkout`). No slot limit. Calls order with retries |
| `order_service.py` | Port 5003, 8 worker slots (`threading.Semaphore`) with a timed wait (1 s or 10 s). Calls payment and inventory with retries. Logs whether it finished after the caller's deadline (the orphan flag) |
| `payment_service.py` | Port 5001, 2 slots, 30-90 ms healthy, 5% errors. The service we usually slow down |
| `inventory_service.py` | Port 5002, same as payment (some dataset runs fault this one instead) |

Each also has `/health` (for start-up checks) and `/stats` (for wait-until-idle).

### 4.5 `code\configs\`: live fault settings

| File | Why it exists |
|---|---|
| `payment_service.json`, `inventory_service.json`, `order_service.json` | The current latency range, error rate and slot wait of each service. `scenario_runner.py` edits them to inject a fault; the service reads them on every request; `run_all.py` resets them to healthy on start. The gateway has none because we never inject faults into it |

### 4.6 `code\tests\`

| File | Why it exists |
|---|---|
| `test_core.py` | 50 unit tests: the four conditions, ρ and window maths, the controller's cut / restore / hold rules, backoff and jitter, the retry budget, the evaluation formulas against scikit-learn, the Alibaba reader. Run with `python -m unittest discover tests` |

### 4.7 `code\data\`: the dataset (raw logs and labels)

78 runs: 28 dataset runs (`ds_01` to `ds_28`) plus 50 experiment runs (`exp_...`). 263,864 call attempts, 71,621 user requests, 335,485 server records.

| File | Why it exists |
|---|---|
| `scenarios.jsonl` | One line per run: its settings and the **exact start and end time of every phase**. This is the ground truth that public traces don't have |
| `telemetry_gateway.jsonl`, `telemetry_order_service.jsonl` | One line per **call attempt** made by that caller: attempt number, backoff before it, response code (0 = timeout), latency, the policy in force, call id. Only the gateway and order make outgoing calls, so there are only two |
| `server_gateway.jsonl`, `server_order_service.jsonl`, `server_payment_service.jsonl`, `server_inventory_service.jsonl` | One line per **request received** by that service: when it arrived, when it got a slot, how long it queued and worked, its status, the caller's deadline, and the `orphaned` flag |
| `user_requests.jsonl` | One line per user request from the load generator: when it was sent, how long it took, success or not |
| `labeled_windows.csv` | Output of `labeling.py` on the 28 dataset runs: one row per edge per 2 s window, with its stats, C1-C4 and the label. This is the training data |
| `labeled_windows_experiment.csv` | The same for the 50 experiment runs (used to count livelock windows per strategy: slide 18 and the report's experiment tables) |
| `baselines.json` | Healthy level of each edge (latency, calls in flight, useful throughput), used to make the ratio features. Also copied into `model.json` for the controller |
| `controller_log.jsonl` | Every controller decision during the experiments (per edge per second: stats, risk, action). Used for the controller trace figure in the report and the replay that found the congestion-gate problem |

### 4.8 `code\results\`: everything the report and slides quote

| File | Why it exists |
|---|---|
| `RESULTS.md` | Every result number in one place, written by the pipeline. If she asks "where does this number come from?", it's here |
| `labeling_dataset.txt` | The detector's report on the dataset (131 windows, 0 false alarms, 9% vs 68%) |
| `labeling_experiment.txt` | The detector's report on the experiment runs, per strategy |
| `sensitivity.txt`, `sensitivity.csv` | The threshold sweep (report, sensitivity table), readable and as a table |
| `model.json` | The trained model as plain numbers: feature names, means, standard deviations, weights, intercept, threshold 0.5, baselines. The controller loads this |
| `prediction_oof.csv` | The out-of-fold risk for every one of the 1,458 windows, scored by a model that never saw its run. Input to `evaluate_model.py` and the lead-time figure |
| `prediction_results.txt` | Prediction results in readable form: model vs baselines, ablations, lead time, self-check (slide 12 and the report's prediction tables) |
| `prediction_metrics.json` | The same numbers in machine-readable form, used by `make_figures.py` and `RESULTS.md` |
| `model_evaluation.txt` | The full evaluation in readable form (the report's evaluation tables) |
| `model_evaluation.json` | The same, machine-readable |
| `experiment_runs.csv` | One row per experiment run (50 rows) with every measured value |
| `experiment_summary.txt` | Mean ± std per strategy and Welch t-tests (slide 18 and the report's experiment tables) |
| `data_validation.txt` | The validation report, all 6 groups PASS (slide 8, report Section VI) |

### 4.9 `code\results\figures\`

Most of these are figures in the report (`Report_IEEE.pdf`); the other two are listed in `RESULTS.md`. The report draws its own plots with `report_plots.py` (same data, IEEE print style); the deck uses screenshots and its own diagrams.

| File | What it shows |
|---|---|
| `fig1_metastable_timeline.png` | same fault, three strategies: users served and internal calls over time |
| `fig1b_error_burst_timeline.png` | the error-burst run over time (in `RESULTS.md`) |
| `fig2_busy_no_progress.png` | static retries: slots 100% busy, work orphaned, nobody served |
| `fig3_experiment_summary.png` | all five strategies under both faults, bar charts |
| `fig4_detection_example.png` | run ds_06 in detail with the livelock windows |
| `fig5_prediction.png` | prediction results overview (in `RESULTS.md`) |
| `fig5b_lead_time.png` | seconds of warning before each livelock episode |
| `fig5c_model_evaluation.png` | ROC, precision-recall, calibration, threshold (redrawn by `evaluate_model.py`) |
| `fig6_controller_trace.png` | what the controller did in one run |
| `graph_snapshots.png` | three snapshots of the service graph |

### 4.10 `code\results\history\`

| File | Why it exists |
|---|---|
| `v1_experiment_runs_predictive_without_gate.csv`, `v1_experiment_summary_predictive_without_gate.txt` | Results of our **first** predictive controller, before the congestion gate. Kept as evidence for the congestion-gate lesson (52.7% in the error burst) and to show we didn't hide the failed version. Kept by hand; no script writes them |

### 4.11 `docs\`

| File | Why it exists |
|---|---|
| `VIVA_QA.md` | About 60 likely questions with answers, grouped by topic. Read it the night before |
| `CODE_WALKTHROUGH.md` | Follows one request through the code, then one analysis. Read it if she asks you to open a file and explain it |
| `DATASET_SEARCH.md` | The public datasets we checked, what each lacks, links, and the one sentence to say |

### 4.12 Files you may see that are made automatically

| File / folder | Why it exists |
|---|---|
| `__pycache__\` (in `code`, `code\common`, `code\tests`) | Python's compiled cache, made automatically when scripts run. Harmless, ignored by git, safe to delete |
| `code\data\retry_policy.json` | Appears when the services or the controller run: the current retry policy per edge. Rewritten all the time, so git ignores it |
| `code\data\*.tmp` | Half-written temp files from the atomic write; normally gone in milliseconds |
| `code\data\graph_snapshots.pkl` | Made by `graph_builder.py` (the snapshots before drawing); can be regenerated, so not kept |
| `code\results\logs\` | Full output of each pipeline step, made by `run_pipeline.py` |
| `code\data_previous\`, `code\results_previous\` | The pipeline's backup of the previous run, made before it starts a fresh one |

---

## Part 5. Cheat sheet: numbers to know

**Testbed**
- 4 processes: gateway :5000 (no limit), order :5003 (8 slots), payment :5001 and inventory :5002 (2 slots each).
- 3 attempts per edge, 2 s timeout, backoff 100 → 200 ms with jitter. User timeout 15 s.
- Slot wait 1 s (fault-coupled) or 10 s (self-sustaining).
- Up to 9 calls to each leaf per user click.

**Capacity inequality**
- μ ≈ 8 / 0.25 = 32/s. λ = 20/s. With 3 attempts up to 60/s > 32: never drains. With 1 attempt: 20 < 32, drains at 12/s; about 16 s for 200 requests (measured 11.4 s).

**Dataset**
- 28 dataset runs (24 random + 4 controls), seed 7, 22.9 minutes. 78 runs in total with the experiments.
- 263,864 call attempts. 6 of 6 validation groups pass. 3 regenerations: 132 / 135 / 131 livelock windows.

**Detection**
- 131 livelock windows of 1,878. 0 false alarms in 636 healthy windows (and 0 in 750 in the experiments).
- Users in flagged windows: 9.0% success, against 68.1% in other bad windows.

**Prediction** (grouped 5-fold CV, unseen scenarios; 1,458 examples, 216 positive = 14.8%)
- F1 0.75, precision 0.68, recall 0.85, PR-AUC 0.84, ROC-AUC 0.95, MCC 0.71 (95%: 0.61-0.79), balanced accuracy 0.89, accuracy 0.92.
- Before onset: F1 0.66, PR-AUC 0.74 (0.63 without graph), MCC 0.63 (reactive rule 0.30).
- Confusion: TP 183, FN 33, FP 86, TN 1,156.
- Warned before 30 of 30 episodes, median 8 s (reactive: 24 of 30, 2 s). Healthy alarms 1.4% (8 of 576).
- Random forest F1 0.80, PR-AUC 0.88. Shuffled control F1 0.14.
- Calibration: Brier 0.072, ECE 0.10.
- Gains with 95% intervals: over reactive rule before onset +0.34 (+0.23 to +0.42); graph features +0.10 (+0.02 to +0.19).
- Cost: 5.2 µs per prediction by hand, 30.3 µs per controller round (0.003% of 1 s). Forest 12,837 µs, about 2,500× slower, about 1 MB. Our model: 49 numbers, 2.2 KB.

**Metastable experiment** (5 runs each)

| | healthy | after fault | recovered | time | calls during fault | livelock |
|---|---|---|---|---|---|---|
| no retry | 88.4% | 46.5% | 5/5 | 11.4 s | 664 | 0/5 |
| static | 100% | 1.7% | 0/5 | never | 1,212 | 5/5 |
| budget 10% | 100% | 26.8% | 3/5 | 20.0 s | 683 | 0/5 |
| reactive | 100% | 33.0% | 4/5 | 17.4 s | 974 | 5/5 |
| predictive | 100% | 44.8% | 5/5 | 14.6 s | 661 | 0/5 |

- Orphaned after fault with static retries: 98.9%.
- Predictive vs static: p = 0.001 (success), 0.003 (recovery time). Vs budget: p ≈ 0.06-0.09 (trend only). Vs reactive: not significant on success (p = 0.25); significant on calls (p < 0.001).
- First cut: predictive 11.5 s in all 5 runs (1.5 s into the ramp); reactive 17.5-19.5 s.

**Error burst**: static 73.2%, predictive 70.0% (p = 0.21, same), reactive 70.4%, budget 54.3%, no retry 39.7%. Predictive calls per success 5.3 (fewest). p95 latency 3.2 s vs 0.7 s for the budget.

**Gate lesson**: v1 got 52.7% in the error burst; 144 of 152 model-only cuts had no congestion; strongest weight is the fresh-call failure ratio (+1.89).

**Live demo (our recorded runs)**: run 1: recovery 1.3%, 3,112 calls, 444.6 calls/success, 98.9% orphaned, last 5 s 0 of 80. Run 2: first CUT at t = 13 s with ρ = 0.07, recovery 23.4%, 16.3 calls/success, last 5 s 78 of 80.

---

## Part 6. Tough questions, short answers

**"Where is the OS in this?"** Livelock, counting semaphores, bounded waiting, processes and IPC, queueing theory, AIMD feedback control. It's the classic OS livelock between services instead of threads (Part 1.3).

**"Your dataset is fake."** It's generated, not fake: real processes, real HTTP, real threads. We inject only the fault; the storm emerges on its own. No public dataset has per-attempt retry data (slide 8), and the OSDI 2022 paper that defined this failure class generated its data the same way. We validated it with 6 groups of checks.

**"You defined livelock yourself, so of course you detect it."** There is no external ground truth for livelock anywhere, so it has to be defined. We checked the definition three ways: users in flagged windows succeed 9% vs 68%; flags appear only during faults and stuck recoveries, never in healthy traffic; and the result doesn't hinge on any threshold (sensitivity sweep).

**"92% accuracy is just class imbalance."** Correct, that's why we lead with MCC 0.71 and balanced accuracy 0.89. An always-"no" model gets MCC 0 and balanced accuracy 0.5.

**"The random forest is better. Why not use it?"** It's a bit better (0.80 vs 0.75 F1) but about 2,500 times slower per prediction and about 1 MB; the controller runs the model every second for every edge. Logistic regression is 49 numbers we compute by hand in 5 µs. A stronger model is the next step.

**"Why not just turn retries off?"** It loses about 12% of users when healthy and is the worst strategy in an error burst (40% vs 70%).

**"Google/AWS already have retry budgets."** A fixed budget is safe in the metastable case but throws away useful retries in an error burst (54% vs 70%). It can't tell good retries from bad. Ours adapts per edge and acts before the storm.

**"Did you tune until it won?"** Thresholds are round numbers fixed in advance; the sensitivity sweep shows they don't matter much; the decision threshold 0.5 was fixed before evaluation; the congestion gate was designed on a replay and all final numbers come from new runs; the failed first version is kept in `results/history/`.

**"Is 5 runs enough?"** For the big effects, yes (p ≤ 0.003 against static). For the small ones, no, and we don't claim them.

**"How is this different from RetryGuard?"** RetryGuard reacts after rejections rise and switches retries off per service. We predict 2-12 s ahead, cut per call edge in graded steps, have a formal livelock test, and measure orphaned work.

**"What if the controller itself is wrong?"** A wrong cut costs a few requests a retry would have saved; the controller restores step by step after 3 calm checks. When it stops, it puts the original policy back. And the reactive safety net is always on.

**"What would you do with more time?"** Part 1.10.

For anything else, `docs/VIVA_QA.md` has about 60 questions with answers.
