# Predicting and Pre-empting Retry-Storm Livelock in Microservices

BCSE303L Operating Systems course project. A working microservice testbed in which retry storms
happen for real, plus everything needed to **detect** them (four formal livelock
conditions), **predict** them seconds ahead (a model over the service graph), and
**pre-empt** them (a retry-budget controller that acts on the prediction).

`QUICKSTART.md` has the demo commands. `results/RESULTS.md` has every number. This file explains the project.

---

## 1. The problem

When a service gets slow or starts failing, its callers retry. Retries are extra load
on a service that's already struggling, so more calls fail, which triggers more retries.
The system ends up **fully busy while achieving almost nothing**. Nothing is blocked
and every thread is running, yet no useful work gets done. That's **livelock**.

In the worst case the storm **outlives the fault**: the slow service recovers, but the
system stays stuck because it's busy serving requests that nobody is waiting for any more.

## 2. Why this is an Operating Systems problem

| OS concept (BCSE303L) | Where it appears here |
|---|---|
| Livelock (Module 3) | Formal four-condition definition, applied per call edge |
| Receive livelock (Mogul & Ramakrishnan, 1997) | Same structure: the system spends all its capacity on work that is thrown away before it completes |
| Counting semaphores (Module 3) | Every service has a fixed number of worker slots, implemented with `threading.Semaphore` |
| Bounded waiting / queues (Module 2) | Requests wait for a slot for at most `queue_wait_sec`. Changing that one bound decides whether the storm can sustain itself |
| Queueing theory, Utilization Law | Used to check the data: a service with c slots can never be more than 100% busy, and it saturates at c / service time |
| Feedback control / congestion control | The controller cuts retries fast and gives them back slowly (AIMD, as in TCP) |

## 3. The testbed

```
user (open-loop Poisson load)
  -> gateway            no slot limit, retries order_service
      -> order_service       8 slots, queue wait 1s (short) or 10s (long), retries both:
          -> payment_service     2 slots, queue wait 1s
          -> inventory_service   2 slots, queue wait 1s
```

- **Real system, not a simulation.** These are 4 Flask processes making real HTTP calls,
  with real threads, real semaphores and real timeouts.
- **Retries at every layer.** By default every call retries up to 3 attempts with exponential
  backoff (100, 200 ms) and **jitter**, and each attempt has a 2 s timeout. One user request can
  become 3 × 3 = 9 calls to a leaf service.
- **Finite capacity.** A request holds a slot for its whole duration, so retries compete with
  fresh requests for the same slots.
- **Faults are injected, storms are not.** `scenario_runner.py` changes only the faulty service's
  latency or error rate. Everything else (amplification, queue build-up, collapse) emerges on its own.
- **Open-loop traffic.** Users keep arriving at the same rate however slow the system gets,
  exactly like real users. A closed loop would slow itself down and hide the storm.

## 4. Two kinds of retry-storm livelock

**Fault-coupled** (order_service queue wait = 1 s): severe while the fault lasts, then gone a
few seconds after it's removed.

**Self-sustaining / metastable** (queue wait = 10 s): the storm continues after the fault is removed.
The mechanism, which you can verify in `fig2_busy_no_progress.png`:

1. Payment slows down, so order_service's queue fills up.
2. The gateway gives up after 2 s and retries. But the abandoned request **stays in order_service's
   queue** and is still processed later, for nobody. That's an *orphaned* request.
3. After the fault is removed, order_service can complete about **32 requests/s**
   (8 slots ÷ 0.25 s). Users arrive at **20/s**. With 3 gateway attempts per user, up to **60/s**
   arrive. Since 60 > 32, the backlog can never drain, every new request waits behind orphans and
   times out, and it becomes an orphan too.
4. Without retries, 20 < 32, so the backlog drains (≈ 200 requests ÷ 12/s ≈ 16 s) and the system
   recovers on its own.

So the system is **bistable**. The same healthy service can sit in a good state or a stuck
state, and retries decide which one. The exit condition is a single inequality:
`arrival rate × retry amplification < capacity`.

## 5. Modules and files

| # | Module | Files |
|---|---|---|
| 1 | Microservice testbed | `services/*.py`, `run_all.py` |
| 2 | Retry + fault injection | `common/retry_client.py`, `common/fault_injection.py`, `scenario_runner.py` |
| 3 | Telemetry pipeline | `common/telemetry.py` (per-attempt, per-request and per-user logs) |
| 4 | Livelock detection / labelling | `labeling.py` |
| 5 | Time-varying service graph | `graph_builder.py`, graph features in `features.py` |
| 6 | Prediction model | `prediction_model.py`, `features.py` |
| 6b | Full model evaluation (all metrics, confidence intervals, run-time cost) | `evaluate_model.py` |
| 6c | Confusion matrix and accuracy, every metric worked out step by step in plain Python | `show_metrics.py` |
| 7 | Retry-budget controller | `controller.py`, `common/retry_policy.py` |
| - | Dataset generation | `generate_dataset.py` |
| - | Experiments (5 strategies × 2 faults × 5 runs) | `experiment_arms.py` |
| - | Data validation | `validate_data.py` |
| - | Optional: comparison with the Alibaba production trace (not used in the results) | `calibrate_from_trace.py` |
| - | Figures, one-command pipeline | `make_figures.py`, `report_plots.py` (the report's plots), `run_pipeline.py` |
| - | Unit tests (50) | `tests/test_core.py` |
| - | Single-run report | `analyze_run.py` |

Shared settings (ports, topology, default configs, retry defaults) live in `common/config.py`.
File writes that several processes share go through `common/safe_files.py`.

## 6. How to run

```
pip install -r requirements.txt
python -m unittest discover tests     # 50 tests, under a second
python run_pipeline.py                # everything from scratch, about 1.5 hours
python run_pipeline.py --quick        # smaller version, about 20 minutes
```

To run steps one at a time, or do a live demo, see `QUICKSTART.md`.

## 7. The dataset

### What is recorded

| File (in `data/`) | One line per | Key fields |
|---|---|---|
| `telemetry_<caller>.jsonl` | call **attempt** | caller, callee, attempt number, call id, response code (0 = timeout), latency, backoff, policy in force, budget blocked |
| `server_<service>.jsonl` | request a service **received** | arrival, slot acquired, finished, status, caller's deadline, **orphaned** (finished after the caller gave up) |
| `user_requests.jsonl` | user checkout | start, end, success |
| `scenarios.jsonl` | scenario run | all settings, plus the exact start and end of every phase (ground truth) |
| `labeled_windows.csv` | edge × 2 s window | all statistics, the four conditions, the label |

### Why it is generated

No public dataset has what this problem needs: **per-attempt retry information** and **fault
ground truth** together, let alone livelock labels. We checked:

| Dataset | What it lacks |
|---|---|
| Alibaba microservice traces 2021/2022 | no attempt number, no status code (confirmed by its README) |
| Uber "Tale of Errors in Microservices" (SIGMETRICS 2025) | error tags but no attempt field; 35 GB+ compressed |
| Meta Canopy, Uber CRISP traces | no retry or outcome fields |
| RCAEval, Eadro, AnoMod (fault-injection benchmarks) | no retry attempts, no livelock labels |

The paper that established this failure class, *Metastable Failures in the Wild* (OSDI 2022),
generated its retry-storm data the same way: it ran a real system, injected a trigger, and logged
timestamp, retry attempts, success/error and latency for every request (their code is public).

### How it is generated

`generate_dataset.py` runs **24 fault scenarios with randomly drawn settings** (seeded, so they're
reproducible), plus **4 no-fault control runs** (two at normal load, two at high load):

- fault kind: latency or error
- target: payment or inventory
- order queue: 1 s or 4–10 s
- load: 12–22 users/s
- shape: gradual ramp (80%) or sudden spike
- severity: 500–900 ms or 60–90% errors

### Why it can be trusted (`validate_data.py`, all checks pass)

1. **Nothing lost.** User requests sent equals user requests logged. Requests a service received
   equals first attempts its caller logged. Both are exact, in every scenario.
2. **Retry rules hold.** Every logical call's attempts are numbered 1, 2, 3 in order. None exceeds
   its policy's limit, and none is retried after succeeding.
3. **Queueing theory holds.** No service is ever more than 100% busy (Utilization Law). Under a held
   latency fault, measured throughput matches the theoretical c / S limit (2 slots ÷ ~800 ms ≈ 2.5/s).
4. **The fault injector does what it is told.** In every run, measured latency and error rate are within
   normal statistical noise (a few standard errors) of the configured values, and healthy services
   fail 5% as configured.
5. **The load generator is accurate.** Achieved arrival rate is within ~2% of target.
6. **Timestamps are consistent.** Nothing ends before it starts.

Optional, not used in any result: `calibrate_from_trace.py` can compare the testbed's call
depth and latencies with the Alibaba production trace, if you download one `MSCallGraph_0.tar.gz`
(~170 MB). The trace has no retry or outcome fields, so it could only ever be a sanity check.

## 8. Detection: the four conditions

For every call edge *e* and every 2-second window *w*:

| Symbol | Meaning |
|---|---|
| λ_base, λ_retry | fresh calls/s, retries/s ending in the window |
| ρ = λ_retry / λ_base | **retry amplification** (capped at 10) |
| S | useful throughput: successful attempts/s |
| N | calls in flight (time-average, from exact overlaps) |
| P | success fraction of attempts |
| S\*, N\* | this edge's healthy level: median over the first 8 s of the same run |

| Condition | Formal test | Meaning |
|---|---|---|
| C1 retry amplification | ρ > 1 | more retries than fresh calls |
| C2 throughput collapse | S < 0.5 · S\* | useful work below half of healthy |
| C3 not draining | N > max(2N\*, 1) and N(w) ≥ 0.8 · N(w−2) | work in flight high and not falling |
| C4 no damping | λ_retry(w) ≥ 0.8 · λ_retry(w−2) and P(w) ≤ P(w−2) + 0.05 | the retry loop isn't dying out |

A window is **livelock** when C1–C4 all hold for **2 windows in a row** (and the edge saw at least 4 calls).

- C1 and C4 separate livelock from plain **overload**. A system with no retries can be
  overloaded and useless, but it isn't livelocked. In the experiments, the no-retry arm gets
  0 livelock windows, as it should.
- C4 alone is often true on healthy traffic (a steady trickle of retries is "not falling"),
  which is why all four are required.
- The 20% tolerance in C3/C4 matters. Without it (as in Review 2), a steady storm flips C4 on and
  off at random and most livelock windows are missed. See the sensitivity table.
- Every threshold was moved up and down (`results/sensitivity.txt`): false alarms on healthy traffic
  stay at 0 throughout, and detection counts change smoothly.

## 9. Prediction

**Question, for every edge every 2 s:** will this edge be in livelock at some point in the next 2–12 seconds?

**Features (`features.py`):**
- this edge now: ρ, fresh-call failure ratio, overload ratio (503s + timeouts), latency and concurrency relative to healthy
- **temporal:** the change since the previous window
- **graph:** one round of hand-written message passing, meaning the worst values of the edges directly below and the values of the edge directly above
- which edge it is

**Model:** logistic regression (class-balanced, standardised features). It is saved as plain
numbers in `results/model.json`, so the controller computes the risk with a weighted sum and a sigmoid,
with no scikit-learn needed at runtime. A self-check confirms the hand computation matches scikit-learn.

**Evaluation:**
- 5-fold cross-validation **grouped by scenario**: test runs are never seen in training, not even other windows of the same run
- baselines: the reactive rule, "livelock now" persistence, and a shuffled-label control
- ablations: without the graph features, without the temporal features
- a random forest for comparison
- **onset-only** evaluation (livelock that hasn't started yet)
- **lead time** before each livelock episode

**Full evaluation (`evaluate_model.py`)** looks at the same out-of-fold scores from every side:
- everything the confusion matrix gives: accuracy, balanced accuracy, precision, recall,
  specificity, F1, F2, **MCC** and Cohen's kappa (MCC and balanced accuracy are the fair ones
  when only 15% of windows are positive; plain accuracy is not)
- ranking quality with no threshold: **ROC-AUC** and PR-AUC
- **calibration**: Brier score, log loss, expected calibration error, reliability table
- **95% bootstrap confidence intervals**, resampling whole scenarios (windows of one run are not
  independent), including for the *differences* between methods
- a threshold sweep (0.1 to 0.9), and where the model works: per edge, per fault kind, per queue length
- the **run-time cost**, because the controller calls the model every second: time per
  prediction, time per controller round, training time and model size, against the random forest
- the shuffled-label control repeated with 20 different shuffles

## 10. The controller

Every second, for every edge, it cuts, holds or restores that edge's retry budget
(`max_attempts` and backoff) through `data/retry_policy.json`, which the services read on every call.

- **Cut** (attempts −1, never below 1; backoff ×2, at most ×4). In reactive mode it cuts when a
  storm is visible now, meaning ρ > 1 **and** the callee is overloaded (>20% 503s or timeouts).
  In predictive mode it cuts when the model's risk ≥ its threshold (0.65) **and congestion is starting** below the
  edge (the callee's response time has at least doubled, or ≥10% overload), with the reactive rule
  kept as a safety net. The model says *when*; the congestion gate makes sure there's a queue to protect.
- **Path rule.** Retries multiply down a chain, so cutting an edge also cuts the edge above it.
- **Restore** (one step at a time) only after the edge has been free of overload for 3 checks in a row
  and at least 4 s after the last cut. It deliberately does **not** use ρ: once retries are off,
  ρ = 0 by definition, and restoring on "low ρ" makes the controller flap on and off while the callee
  is still drowning. That was a bug in the Review-2 version.
- Errors without overload (500s with free slots) don't block a restore, because then a retry is a
  fresh chance to succeed and uses capacity nobody else needs.
- This is TCP's AIMD shape: cut fast on congestion, give back slowly.
- The controller always puts the original policy back when it stops.

## 11. Experiments (`experiment_arms.py`)

The same fault is applied under five retry strategies, **5 runs each, in shuffled order**:

| Arm | Policy |
|---|---|
| no_retry | 1 attempt everywhere |
| static_retry | 3 attempts everywhere (common default) |
| retry_budget | 3 attempts, but retries capped at 10% of fresh calls per edge (Google SRE book) |
| reactive | our controller, reactive mode |
| predictive | our controller, predictive mode |

| Fault | Setting | Question it answers |
|---|---|---|
| **metastable** | payment latency ramps to ~800 ms, order queue 10 s | does the system recover after the fault? |
| **error_burst** | payment error rate ramps to 70%, order queue 1 s | retries are *useful* here; does a strategy keep that benefit? |

**Metrics:**
- user success % (healthy / during the fault / after the fault is removed)
- internal calls (work done)
- calls per successful user request
- orphaned % (share of order_service's finished work that the gateway had already abandoned)
- p95 latency
- **recovery time**: seconds after the fault is removed until users get ≥ 90% of that run's
  own healthy success rate, sustained for 5 s

## 12. Main results

See `results/RESULTS.md` for every number (generated by the pipeline). Summary:

**Dataset and detection** (28 runs: 24 fault + 4 no-fault controls; 21,662 user requests; 91,693 call attempts)
- **131 livelock windows** found, with **0 false alarms in 636 healthy windows**. That includes two
  high-load runs at 26–28 users/s with no fault, so "busy" is never mistaken for "livelocked".
- Users who arrived during a flagged window got a success **9%** of the time, against **68%** in
  other fault-time windows. The label tracks real user harm.
- **Reproducible:** the dataset was generated three times from the same seed (live traffic, random
  timing) and gave 132, 135 and 131 livelock windows, with 0 false alarms every time.
- `validate_data.py`: **every check passes.** Nothing was lost, every retry sequence is valid, no service
  was ever more than 100% busy, saturation throughput matches c/S, and the fault injector matches its
  settings within statistical noise.

**Prediction** (5-fold cross-validation on unseen runs; threshold chosen inside the training folds)

| Method | Accuracy | Precision | Recall | F1 | MCC | PR-AUC | F1 before onset |
|---|---|---|---|---|---|---|---|
| **Our model** (logistic regression, 18 features) | **0.941** | **0.807** | **0.792** | **0.799** | **0.765** | **0.853** | **0.717** |
| without graph features | 0.924 | 0.718 | 0.801 | 0.757 | 0.714 | 0.812 | 0.641 |
| without temporal features | 0.940 | 0.809 | 0.782 | 0.795 | 0.761 | 0.846 | 0.706 |
| random forest, 100 trees (comparison) | 0.941 | 0.825 | 0.764 | 0.793 | 0.760 | 0.883 | 0.693 |
| reactive rule, no learning | 0.898 | 0.715 | 0.523 | 0.604 | 0.556 | – | 0.320 |
| shuffled-label control | 0.160 | 0.130 | 0.819 | 0.224 | −0.195 | 0.121 | 0.150 |

- Confusion matrix: TN 1,201, FP 41, FN 45, TP 171 (1,458 windows, 216 positive).
- The model warned before **30 of 30** livelock episodes, a median **8 s ahead**, with **0** alarms in
  576 healthy windows. The reactive rule warned in 24 of 30, a median 2 s ahead.

**Full evaluation** (`results/model_evaluation.txt`, 95% intervals from 1000 run resamples)

| Metric (our model, all windows) | Value | 95% interval |
|---|---|---|
| Accuracy | 0.941 | 0.922 to 0.960 |
| Precision | 0.807 | 0.687 to 0.884 |
| Recall | 0.792 | 0.712 to 0.871 |
| MCC | 0.765 | 0.681 to 0.834 |
| ROC-AUC | 0.954 | 0.904 to 0.979 |
| PR-AUC | 0.853 | 0.769 to 0.913 |
| Balanced accuracy / specificity | 0.879 / 0.967 | |
| F1 gain over the reactive rule, before onset | +0.40 | +0.34 to +0.45 |
| PR-AUC gain from the graph features, before onset | +0.08 | +0.02 to +0.16 |

- The gains over the rule and from the graph features are real (their intervals stay above 0).
- Calibration: Brier skill 0.49, ECE 0.09. Class-balanced training makes the risk values higher
  than the true frequency; the threshold (0.65) is chosen for these risks by nested cross-validation.
- Cost: one risk value takes about 3 µs and a whole controller round about 27 µs, i.e. 0.003% of the
  1 s control interval. The random forest takes about 6 ms per window and is 0.5 MB against 2.4 KB.
- Weak spot: the order→payment edge has only 5 positive windows, too few to learn or judge.

**Experiments** (5 runs per cell, mean ± std; re-run with the current model)

*Metastable* (payment slows to 800 ms for 20 s, order queue 10 s):

| Strategy | Healthy success | Success after fault | Recovered | Recovery time | Calls during fault | Livelock formed |
|---|---|---|---|---|---|---|
| no retry | 90% | 46 ± 6% | 5/5 | 12.0 ± 1.6 s | 662 | 0/5 runs |
| static retry | 100% | **3 ± 0%** | **0/5** | **never** | 1212 | 5/5 runs (79 windows) |
| retry budget 10% | 100% | 24 ± 13% | 3/5 | 20.4 ± 4.6 s* | 682 | 0/5 runs |
| reactive (ours) | 100% | 41 ± 14% | 5/5 | 14.8 ± 2.8 s | 973 | 5/5 runs (12 windows) |
| **predictive (ours)** | **100%** | **48 ± 10%** | **5/5** | **13.0 ± 2.2 s** | **676** | **1/5 runs (1 window, on order→payment)** |

\* runs that never recovered are counted as the full 25 s window.

*Error burst* (payment fails 70% of calls, fast, order queue 1 s):

| Strategy | Healthy success | Success during fault | Calls per success | p95 latency |
|---|---|---|---|---|
| no retry | 89% | 39 ± 3% | 7.6 | 0.3 s |
| static retry | 100% | 74 ± 4% | 5.7 | 4.0 s |
| retry budget 10% | 100% | 53 ± 4% | 6.6 | 0.6 s |
| reactive (ours) | 100% | 69 ± 4% | 5.7 | 4.0 s |
| **predictive (ours)** | **100%** | **71 ± 5%** | **5.4** | 3.7 s |

**What this shows**

- **Static retries turn a 20-second slowdown into a failure that never ends.** After the fault,
  98% of order_service's work is for requests nobody is waiting for. The same fault with retries
  controlled recovers on its own.
- **No fixed policy is right for both faults.** Static retry is the best during the error burst and
  catastrophic in the metastable case. The 10% budget is decent in the metastable case and loses ~21
  points in the error burst. No-retry recovers fast but loses ~10% of users every healthy minute.
- **The predictive controller is the only strategy that is never bad.** It keeps 100% in healthy
  periods, recovered in 5/5 runs, fastest (13.0 s), served the most users after the fault (48%),
  made 44% fewer calls than static retry during the fault, and the self-sustaining livelock on
  gateway→order never formed (one run had a single 2 s livelock window on order→payment during the
  fault, which the safety net cut). In the error burst it kept static-level success (71% vs 74%,
  difference not significant) with the fewest calls per success.
- **Statistics:** against static retry the differences are clear (p = 0.001 for success after the
  fault). Against the budget: p = 0.011 in the metastable case and p < 0.001 in the error burst.
  Reactive vs predictive is not significant with 5 runs (p = 0.34), so we don't claim it.

## 13. What changed since Review 2

| Problem in the Review-2 build | Fix |
|---|---|
| Controller restored retries when ρ dropped, but ρ = 0 by definition at 1 attempt, so it flapped during the fault | Restore on overload clearing plus hold time (unit-tested) |
| Load generator waited for each phase's requests before starting the next, giving a free pause at fault removal | Continuous open-loop Poisson traffic |
| README said self-sustaining livelock was structurally impossible | It needs orphaned work (a long queue). Now demonstrated and explained with the capacity inequality |
| C4 used strict trend tests, a coin flip in a steady storm, so most fault windows were missed | 20% noise tolerance, justified by the sensitivity sweep |
| C3 used an absolute threshold (3 calls in flight) for every edge | Relative to each edge's own healthy level |
| Windows spanned all runs and idle gaps (42 empty windows per edge went into the model); trends compared across runs | Per-scenario, per-edge windows; empty windows excluded |
| Model evaluated on a time split of mixed runs with 18 positives and no baseline | Grouped CV by scenario, baselines, ablations, onset-only, lead time |
| The graph was drawn but never used by the model | Graph features (message passing) plus an ablation |
| The controller didn't use the prediction at all | Predictive mode |
| Baselines were only "no retry" and "always retry" | Added the Google-SRE retry budget |
| One run per arm | 5 runs, shuffled order, mean ± std, Welch t-test |
| No jitter in backoff (unfair baseline) | Equal jitter, as in production clients |
| Config and policy files written in place: torn reads, and on Windows the controller could crash on file replace | Atomic writes with retry, plus last-good fallback |
| Ctrl+C mid-fault left the system broken for the next run | try/finally restore; services reset on start; interrupted runs marked and ignored |
| Flask logged every request to the console (slow on Windows, distorts latency) | Turned off |
| Leftover in-flight work leaked into the next run | Wait until all services report 0 in flight (`/stats`) |
| Controller re-read whole log files every 2 s | Incremental tail of new lines only |
| Wasted work was never measured directly | Deadline header gives an orphaned-work metric |
| Numbers differed between README, deck and outputs | One source: `results/RESULTS.md`, regenerated by the pipeline |
| Busy ports were ignored (experiments could hit stale processes) | Refuse to start |
| Scripts depended on the current folder | Absolute paths from `common/config.py` |
| Model judged on precision / recall / F1 only | Full evaluation: MCC, ROC-AUC, calibration, bootstrap confidence intervals, threshold sweep, per-edge results, run-time cost (`evaluate_model.py`) |
| No tests, no data validation | 58 unit tests, `validate_data.py` |

### A lesson from the experiments: the congestion gate

The first version of predictive mode cut whenever the model's risk was ≥ 0.5. In the metastable
experiment that worked well. In the error burst, though, it kept only **52.7%** of users served,
against **68.1%** for reactive mode. That's no better than the fixed budget.

Replaying the logged decisions showed why: the model's strongest input is the fresh-call
failure ratio, so it read "payment is returning errors" as "livelock coming", even though payment
was answering fast with free slots. In that situation a retry is a useful second chance.

The fix applies the controller's own principle (*cut on congestion, not on errors*) to the
predictive mode too: a predicted cut also needs the callee's response time to have at least doubled,
or ≥10% overload. In the replay this removed 144 of 152 unnecessary cuts in the error burst and kept
all early cuts in the metastable runs. The threshold was chosen on that replay, so the numbers in
section 12 come from **completely new runs**. The first version's results are kept in
`results/history/` for comparison.

## 14. Limitations (say these before you are asked)

- **One machine, Python threads, Flask dev server.** Absolute numbers (capacities, latencies) belong
  to this testbed. The mechanisms (queueing, amplification, orphaned work) are general.
- **A 2-level call chain.** Real chains are deeper and amplification multiplies per layer, so
  our numbers are a lower bound.
- **Labels come from our own formal definition.** No external livelock ground truth exists anywhere.
  We validate the labels against what users experienced, against the injected fault timeline,
  and with a sensitivity sweep.
- **The model is logistic regression on hand-built features, not the proposed temporal GNN.** The
  temporal features add very little, and a random forest ranks slightly better (PR-AUC 0.88 vs 0.85)
  but is no better at its threshold (F1 0.79 vs 0.80), so a learned temporal model is unproven here.
- **The model is trained on data recorded under the static retry policy.** At runtime the controller
  changes the policy, which shifts what the model sees. That's mitigated by the reactive safety net
  and by restoring on overload, not on the model.
- **5 repetitions per arm.** p-values are reported but should be read with care.
- **The risk values are not calibrated probabilities.** Class-balanced training pushes them up
  (a risk of 0.6-0.8 is followed by livelock about 40% of the time). The threshold is chosen for
  these risks, but "risk 0.8" should not be read as "80% sure".
- **The congestion gate's thresholds** (2× response time, 10% overload) were chosen by replaying the
  first experiment's logs, then evaluated on fresh runs.
- **For an edge whose callee calls others**, the gate looks at the edges below it (order_service's own
  response time includes its retry back-offs). A fault inside order_service itself would therefore only
  be caught by the reactive safety net, not pre-empted.
- **The retry budget baseline uses one ratio (10%).** Other ratios trade differently.
- **Orphaned-work timing relies on one shared clock** (fine on one machine; real systems need
  synchronised clocks).
- **Not implemented:** deadline propagation and LIFO/CoDel queueing, which drop orphaned requests at
  the queue. They would shorten recovery further and are complementary to retry control.

## 15. References

1. J. Mogul, K. K. Ramakrishnan. *Eliminating Receive Livelock in an Interrupt-Driven Kernel.* ACM TOCS 15(3), 1997.
2. N. Bronson et al. *Metastable Failures in Distributed Systems.* HotOS 2021.
3. L. Huang et al. *Metastable Failures in the Wild.* OSDI 2022. Code: github.com/lexiangh/Metastability
4. A. Farahbakhsh et al. *Characterizing Metastable Faults and Failures.* arXiv 2606.00942, 2026.
5. Y. Gan et al. *Seer: Leveraging Big Data to Navigate the Complexity of Performance Debugging in Cloud Microservices.* ASPLOS 2019.
6. B. Beyer et al. (eds). *Site Reliability Engineering*, chapter "Handling Overload" (retry budgets). O'Reilly, 2016.
7. S. Luo et al. *Characterizing Microservice Dependency and Performance: Alibaba Trace Analysis.* SoCC 2021.
8. I.-T. A. Lee, Z. Zhang, A. Parwal, M. Chabbi. *The Tale of Errors in Microservices.* SIGMETRICS 2025.
9. M. Brooker. *Exponential Backoff and Jitter.* AWS Architecture Blog, 2015.
10. V. Jacobson. *Congestion Avoidance and Control.* SIGCOMM 1988.
