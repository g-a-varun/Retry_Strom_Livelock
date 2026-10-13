# Viva question bank: Retry-Storm Livelock

How to use this: read each answer once, then try answering out loud **without looking**.
The answers are short on purpose; each one ends where a follow-up question usually starts.
Every number quoted here comes from `code/results/RESULTS.md`.

---

## A. The problem

**Q1. Explain the problem in one minute.**
A service gets slow. Its callers time out and retry. Retries are extra load on the
already-slow service, so more calls fail and more retries follow. The system ends up
100% busy but users get almost nothing. That's livelock: everything is running, but no
progress is made. In the worst case it continues *after* the slow service has recovered.

**Q2. How is livelock different from deadlock?**
In deadlock, processes are *blocked*, each waiting for a resource another holds, and the CPU
is idle. In livelock, processes are *running* and changing state in response to each other,
but none makes progress, and the CPU is busy. Our system is the second kind: order_service's
slots are 100% busy, yet users get ~0% success.

**Q3. How is it different from plain overload?**
Overload means demand is higher than capacity. Livelock adds a **feedback loop**: the failure
itself creates more demand (retries), and the capacity is spent on work that's thrown away.
That's why we require C1 (retry amplification) and C4 (the loop isn't dying out). The
no-retry arm is overloaded during the fault too, with near-zero success, but our detector
gives it **0 livelock windows**, because there is no retry loop.

**Q4. Where is the OS in this?**
- **Livelock** is a Module 3 concept. We give it a measurable, formal definition.
- **Worker slots** are counting semaphores (`threading.Semaphore`), and the queue wait is a
  bounded wait on that semaphore.
- **The closest classic OS result is receive livelock** (Mogul & Ramakrishnan, 1997). An
  interrupt-driven kernel under heavy network load spends all its CPU on interrupts for
  packets that are later dropped, so throughput falls to zero while the CPU is 100% busy.
  Our order_service does the same with orphaned requests.
- The fix has the shape of TCP congestion control (AIMD).

**Q5. Is this a real-world problem, or something you made up?**
It's real. Retry storms are a documented cause of major outages. *Metastable Failures in
the Wild* (OSDI 2022) studied public incident reports and found retries are a common
sustaining effect. Uber's study (SIGMETRICS 2025) found requests with errors did 1.9×
more work. Google's SRE book prescribes retry budgets specifically to stop retry storms.

---

## B. Originality

**Q6. What exactly is new here? Google already has retry budgets.**
Three things, together:
1. **A per-edge, testable definition** of retry-induced livelock (four conditions) that
   separates it from plain overload and from a system that is already recovering.
2. **Prediction before it starts**: a model over the service graph warns a median ~8 s before
   onset, where a reactive rule manages ~2 s.
3. **A controller that acts on that prediction, per edge and along the path**, and restores
   on congestion signals rather than on rho, which is what makes it stable.

A retry budget is a fixed ratio applied everywhere. It needs no prediction and has no notion
of *where* the storm is. We compare against it directly, and it's a strong baseline. We
report where it wins and where it loses.

**Q7. Which related work is closest, and how do you differ?**
- **Metastable-failure papers** (Bronson 2021, Huang 2022, Isaacs 2025) name and characterise the failure, but don't detect or predict it online.
- **Seer** (ASPLOS 2019) predicts QoS violations, but doesn't model retries and acts by resizing containers.
- **SRE retry budgets** are fixed and reactive.
- We combine per-attempt retry telemetry, a formal livelock test, prediction, and per-edge control.

**Q8. Did you just tune your controller until it won?**
No. The experiment settings (20 users/s, 10 s queue, 800 ms peak, 70% errors) were fixed
before running. The baselines are standard. The **static retry policy beats our controller
on success during the error burst** (73% vs 70%, not a significant difference), and we report
that. The retry budget comes close to us in some metrics. With 5 runs per arm, we give mean ± std
and p-values, and we don't claim a win where p > 0.05.

---

## C. The dataset (expect the most questions here)

**Q9. Why didn't you use a public dataset?**
No public dataset has what this problem needs. We checked six:
- **Alibaba** traces (2021/2022): no attempt number and no status code. Their README confirms it.
- **Uber's error dataset**: error tags but no attempt field, and over 35 GB.
- **Meta Canopy** and **Uber CRISP**: no retry or outcome fields.
- **RCAEval, Eadro, AnoMod** (fault-injection benchmarks): no retry attempts and no livelock labels.

You can't compute retry amplification without knowing which call is a retry, and you can't
label livelock without knowing when the fault started and ended.

**Q10. So how can a self-generated dataset be trusted?**
Four answers:
1. **It's measured, not invented.** Real HTTP calls, real threads, real semaphores, real
   timeouts. We inject only the fault (a service's latency or error rate). The storm,
   queue growth and collapse emerge from the system; nothing in the code says "make a storm".
2. **It's the accepted method.** The OSDI 2022 paper that defined this failure class
   generated its retry-storm data the same way: it ran a real MongoDB cluster, injected a
   trigger, and logged retry attempts, success/error and latency per request.
3. **It's checked against theory** (`validate_data.py`, every check passes):
   - Nothing is lost: requests sent equals requests logged, and calls received equals first
     attempts logged, exactly.
   - Retry rules are never violated.
   - No service is ever more than 100% busy (Utilization Law).
   - Under a latency fault, measured throughput matches the queueing limit c/S (2 slots ÷ 0.8 s ≈ 2.5/s).
   - Injected latency and error rates match their configured values.
4. **It's varied and reproducible.** There are 24 randomised fault scenarios (2 fault kinds,
   2 targets, 2 queue types, loads 12–22/s, ramps and spikes), plus 4 no-fault control runs,
   all from a fixed seed.

**Q11. Isn't the model just learning your own simulator?**
It learns patterns in measured telemetry from a real running system, and it's tested on
**scenarios it never saw** (grouped cross-validation). The shuffled-label control scores
near chance, so the model isn't exploiting a leak. The honest limit: absolute numbers belong
to this testbed, which is why we report the mechanism (the capacity inequality) and not just numbers.

**Q12. Did you use the Alibaba trace?**
We studied it, but none of our results depend on it. It records who called whom and how
long each call took, but **no attempt numbers and no success/failure**. So it can't show a
retry storm, and it can't be labelled. The most it could do is a sanity check of the testbed's
latencies and call depth. We wrote a script for that (`calibrate_from_trace.py`), but it's
optional. Our claim doesn't rest on matching Alibaba's numbers. It rests on the mechanism (the
capacity inequality in Q17), which holds for any service with finite slots and retries.

**Q12b. Are your results reproducible, or a lucky run?**
The whole dataset was generated three times from the same seed (two development runs and the
final clean run), with live traffic and random timing each time. The conclusions came out the
same: 132, 135 and 131 livelock windows, 0 false alarms every time, and model F1 0.72, 0.72 and
0.75. The experiments are repeated 5 times per strategy in shuffled order.

**Q13. Why is the data split into 2-second windows?**
Long enough to contain several calls per edge at our traffic rates (so ratios like rho aren't
noise), and short enough to see the storm build up (a ramp lasts 6–14 s). The sensitivity
sweep shows the conclusions don't depend on a single magic setting.

**Q14. How many samples do you have?**
The training set is 28 scenarios, 21,662 user requests, 91,693 call attempts, 1,878 labelled
edge-windows (131 livelock) and 1,458 prediction examples (216 positive). The experiments add
50 more runs.

---

## D. The testbed

**Q15. Why only 4 services?**
It's the smallest topology that has the two features a retry storm needs: **retries at two
levels** (so amplification multiplies: 3 × 3 = 9) and **fan-out** (one service calling two).
More services wouldn't change the mechanism, just the scale. Real production call graphs
(like the ones in Alibaba's traces) are often deeper, and retries multiply per level, so a
deeper chain would make the effect stronger. Ours is the conservative case.

**Q16. Why do the slots and the queue matter?**
Without finite slots there's no contention, so retries are just extra traffic that does no harm.
The **queue wait** decides which kind of livelock you get. With a 1 s wait, a request that can't
be served is rejected fast, nothing is processed after the caller gives up, and the storm ends
with the fault. With a 10 s wait, requests sit in the queue after the gateway has given up, get
processed anyway (orphans), and the storm sustains itself.

**Q17. Prove the storm sustains itself, with numbers.**
After the fault is removed, order_service completes about 32 requests/s (8 slots ÷ 0.25 s each).
Users arrive at 20/s. With 3 gateway attempts each, up to 60/s arrive. Since 60 > 32, the queue
never drains. Every new request waits behind orphans, times out, and becomes an orphan itself.
With retries cut to 1, 20 < 32, so the ~200-request backlog drains at 12/s, which takes about
16 s, and that's what we measure.

**Q18. Why open-loop traffic? Why Poisson?**
Real users don't wait for each other: a closed-loop generator would slow down when the system
slows down and hide the storm. Poisson arrivals are the standard model for independent users.

**Q19. Why jitter in the backoff?**
Without jitter, clients that failed together retry together, in lock-step. Every production
client library jitters, so leaving it out would make the static-retry baseline unfairly bad.
We use "equal jitter": half of the backoff is fixed and half is random.

---

## E. Detection

**Q20. State the four conditions.**
For an edge and a 2 s window:
- **C1:** rho > 1, meaning more retries than fresh calls.
- **C2:** useful throughput < half of the edge's healthy level.
- **C3:** calls in flight > 2× healthy, and not falling.
- **C4:** retries not falling and success not improving, compared with 4 s earlier.

All four must hold for 2 windows in a row. "Healthy" is the same edge in the first 8 s of the same run.

**Q21. Why do you need all four? Isn't rho enough?**
No. Each condition alone fires in situations that aren't livelock:
- **rho > 1** happens briefly in healthy bursts.
- **Collapse plus a high in-flight count (C2 + C3)** happens in plain overload, e.g. the no-retry arm.
- **C4 alone** is often true on healthy traffic (a flat trickle of retries).

Only together do they describe "busy, retrying, getting nothing, and not getting better". The
result is 0 false alarms in 636 healthy windows, including two runs at 26–28 users/s with no fault.

**Q22. How did you choose the thresholds?**
Each threshold has a meaning (rho > 1: retries outnumber fresh calls; half of healthy
throughput). Then we moved each one up and down (`sensitivity.txt`). False alarms stay at 0
for every setting, and the number of flagged windows changes smoothly, so the conclusions don't
hinge on one value. The noise tolerance in C3 and C4 is the one that matters most: without it,
detection drops by more than half, because a steady storm flips C4 on and off at random.

**Q23. Why isn't the payment edge red, when payment is the faulty service?**
Because the storm isn't there. order_service has only 8 slots, which caps how many retries can
pile up towards payment, so rho there hovers around 1. The gateway has no limit, so its retries
to order_service pile up to rho ≈ 2 with 100+ calls in flight. The detector points at where the
**storm** is, not where the **fault** is. That's the value of per-edge analysis.

**Q24. How do you know your labels are right, if you defined them yourself?**
There's no external ground truth for livelock anywhere; it has to be defined. We check the
definition three ways:
1. Users who arrived during flagged windows got a success 9% of the time, against 68% in
   other fault-time windows.
2. Flags appear only during faults and stuck recoveries, never in healthy traffic.
3. The sensitivity sweep.

---

## F. The graph

**Q25. Where is the graph used? Isn't it just a picture?**
In Review 2 it *was* just a picture. Now `features.py` does one round of **message passing** over
it. Each edge's feature vector also gets the worst values of the edges directly below it and the
values of the edge directly above it. That's the core operation of a graph neural network, done
by hand. The "without graph features" ablation measures what it adds: a small gain overall
(F1 0.75 vs 0.74), and a clear one on predicting onsets (PR-AUC 0.74 vs 0.63).

**Q26. Why not a real temporal GNN as you proposed?**
Two reasons. First, the honest finding: the **temporal** features (change since the last window)
added nothing measurable, and a random forest only does a little better than logistic regression
(F1 0.80 vs 0.75).
A heavy GNN wouldn't be justified by this data size (about 1,460 examples). Second, logistic
regression can be computed by hand in the controller, with no ML library at runtime. The TGN
stays as future work, motivated by those two results.

---

## G. Prediction

**Q27. What exactly does the model predict?**
For each edge every 2 s: the probability that this edge will be labelled livelock at some
point in the next 2–12 seconds. The label is looked up only in *future* windows, never the current one.

**Q28. How did you avoid data leakage?**
1. Features use only the current and previous windows.
2. Labels come only from future windows, starting one window later.
3. Cross-validation is **grouped by scenario**, so the test runs are never seen in training, not
   even other windows of the same run.
4. The shuffled-label control scores near chance. If something were leaking, it would score well too.

**Q29. Precision is lower than recall. Is that a problem?**
For an early warning it's the right trade. A missed storm costs minutes of outage, while a
false alarm costs a few seconds of fewer retries. On healthy traffic the model raises an alarm
in 1.4% of windows. In the controller, the congestion gate filters those out too.

**Q30. The random forest scores higher. Why not use it?**
It's a few points better. We kept logistic regression because the controller computes it by
hand, because its weights can be inspected, and because the gap is small. We report the forest
as evidence that a non-linear model has headroom.

**Q31. Isn't a reactive rule just as good?**
Overall it isn't far behind, but on the question that matters for pre-emption, *livelock that
hasn't started yet*, it's much worse (onset F1 0.32 vs 0.66). And it warns a median 2 s before
onset (24 of 30 episodes), against 8 s for the model (30 of 30 episodes).

**Q32. Why do some feature weights look backwards (overload has a negative weight)?**
The features are correlated (fresh-call failures include overload failures), so individual
logistic-regression weights can't be read alone. The weight on one feature depends on what the
others already explain. The right test of a feature group is the ablation, not the weight sign.

---

## H. The controller

**Q33. Explain the controller in 30 seconds.**
Every second, for each edge, it measures the last 2 s:
- **Cut:** when the model predicts livelock *and* congestion is starting below the edge, or a storm
  is visible now (rho > 1 and the callee overloaded), it reduces that edge's max attempts by one and
  doubles the backoff. It also cuts the edge above it, because retries multiply down the chain.
- **Restore:** once the edge has had no overload for 3 seconds in a row (and at least 4 s since
  the last cut), it gives back one step.

Cut fast, restore slowly, like TCP.

**Q34. Why not restore when rho is low?**
Because once max attempts is 1, rho is 0 by definition, since no retries are allowed. The
Review-2 controller made exactly this mistake. It cut to 1, saw rho = 0, restored, saw the storm,
cut again, and flapped on and off for the whole fault. Overload (503s and timeouts on fresh
calls) is measured the same way whatever the retry setting, so it's an honest health signal.
There's a unit test for this exact case.

**Q35. Why is the controller allowed to restore while the callee still returns errors?**
If the callee returns 500 errors but has free slots, a retry is a fresh chance to succeed and
uses capacity nobody else needs. Retries only hurt when the callee is out of capacity. That's why
overload, not failure, is the congestion signal.

**Q35b. Why does predictive mode need a "congestion gate"? Isn't the model enough?**
That was a lesson from our own experiments. The first version cut whenever the model's risk was
≥ 0.5. In the error burst it kept only 53% of users served, against 68% for reactive mode, because
the model's strongest input is the failure ratio and it read "payment returns errors" as "livelock
coming". But payment was answering fast with free slots, so a retry was a useful second chance.

Replaying the logged decisions confirmed that 144 of its 152 extra cuts happened with no congestion
at all. So a predicted cut now also needs a sign that the callee side is filling up: response time
at least doubled, or ≥ 10% overload. That's the same principle the reactive mode already follows.
In the replay it kept every early cut in the metastable runs. The final numbers come from completely
new runs, and the first version's results are kept in `results/history/`.

**Q35c. Why does the gate look at the edges *below* the gateway edge, not its own latency?**
order_service's response time includes its own retry back-off sleeps. In an error burst, order gets
slower simply because it's retrying, not because anything is queueing. The edges below it (payment,
inventory) show real queueing.

**Q36. What's the path rule?**
Retries multiply down a chain: 3 gateway attempts × 3 order attempts = 9 payment calls per user.
When a lower edge is in trouble, cutting the edge above it removes that multiplier.

**Q37. What does "predictive" add over "reactive" in practice?**
Timing, and that timing changes the outcome:
- In all 5 metastable runs the predictive controller made its first cut at 11.5 s, just 1.5 s into
  the ramp, while rho was still near 0. The reactive one cut at 17.5–19.5 s, about 7 s later.
- Because fewer retries entered the queue, **the livelock never formed in any of the 5 predictive
  runs (0 windows)**. With reactive it formed in all 5 (12 windows) before being fixed.
- Predictive also used 32% fewer calls during the fault (661 vs 974), recovered in 5/5 runs vs
  4/5, and recovered faster (14.6 vs 17.4 s). That last difference isn't significant with 5 runs
  (p ≈ 0.3), so we don't claim it.

**Q38. What if the controller crashes or is stopped?**
It puts the original policy back when it stops. If it's killed outright, the next experiment
resets the policy, and starting the services always resets every config to healthy. Services
fall back to their last good policy if the file is being rewritten at that instant.

**Q39. Couldn't the controller itself cause harm?**
Yes. A cut that wasn't needed costs requests that a retry would have saved. In the error burst
both controllers end up about 3 points below static retry (70% vs 73%, not significant). The first,
ungated predictive version lost 20 points there, which is why the congestion gate exists (Q35b).
In healthy periods no controller run lost any users (100% in every run).

---

## I. Experiments and results

**Q40. Summarise your results in three sentences.**
With a realistic queue, static retries turn a 20-second slowdown into a storm that never
recovers (0 of 5 runs), while our predictive controller recovers every time (14.6 s) with 100%
success in healthy periods. Retrying is right for error bursts and wrong for saturation, so no
fixed policy is good in both; the predictive controller is the only strategy that is never bad
(70% in the error burst vs 54% for the fixed budget). The model warns about 8 s ahead, and acting
on it prevented the livelock from forming in all 5 runs.

**Q41. Why does no-retry recover as fast as your controller? Why not just turn retries off?**
Because no-retry loses about 11% of users in **healthy** times (ordinary 5% failures are no
longer recovered) and 33 points of success in the error burst (40% vs 73%). Our controller
behaves like no-retry during the storm and like full retries the rest of the time.

**Q42. Why is success during the fault ~5% for everyone in the metastable experiment?**
During the fault payment can do about 2.5 requests/s while users arrive at 20/s, so at most
about 12% could ever be served. No retry policy can create capacity. The differences between
strategies show up in wasted work, latency and, above all, **recovery**.

**Q43. Is 5 runs enough?**
It's enough to see large effects (static retry never recovers, while the others always do), and
we report standard deviations and Welch t-test p-values. For small differences between our
controller and the budget, we say so instead of claiming a win.

**Q44. How is recovery time measured?**
It's the seconds after the fault is removed until users get at least 90% of that run's own
healthy success rate, sustained for 5 s. A run that never gets there is reported as "never".

**Q45. What is "orphaned work"?**
Each caller sends its deadline in a header. When order_service finishes a request after the
gateway's deadline has passed, nobody is waiting for the answer. We count those. Under static
retries, after the fault 99% of order_service's work was orphaned. That's the livelock seen
directly.

---

## J. Limitations and future work

**Q46. What are the weaknesses?**
- It's one machine, so absolute numbers are testbed-specific.
- It's 2 levels deep, so the results are a lower bound.
- The labels come from our own definition (validated, but defined by us).
- The model is logistic regression, not the TGN.
- It's trained under one retry policy, then used under another (distribution shift).
- There are 5 runs per arm.

**Q47. What would you do next?**
1. Run it on Kubernetes with Istio/Envoy, reading Envoy's retry metrics instead of our log files.
2. Add deadline propagation and LIFO queueing, which drop orphans at the queue and are
   complementary to retry control.
3. Try a temporal GNN with longer history, now that the data shows short-term deltas aren't enough.
4. Test deeper call graphs shaped by the Alibaba trace.

---

## K. Code questions (be ready to open the file)

- **Where is rho computed?** `features.py`, `finish_window()`: retries ÷ fresh calls in the window, capped at 10.
- **Where are the four conditions?** `labeling.py`, `check_conditions()`.
- **How do services read the retry policy?** `common/retry_policy.py`, `get_retry_policy()`, read on every call. A half-written file falls back to the last good one.
- **How is a retry done?** `common/retry_client.py`, `call_with_retry()`: attempt, log, maybe back off with jitter, try again.
- **Where are the slots?** In each `services/*.py`, `worker_slots = threading.Semaphore(n)`, used as `worker_slots.acquire(timeout=queue_wait)`.
- **Where is the controller's decision?** `controller.py`, `control_step()`, which is unit-tested.
- **How does the controller read logs quickly?** `common/telemetry.py`, `read_new_call_records()`: it remembers its byte offset in each file and reads only new lines, holding back a half-written last line.
- **How is the model used live?** `features.py`, `risk_probability()`: a weighted sum and a sigmoid, checked against scikit-learn to within 1e-15.

## L. Gotcha questions

**"Your livelock disappears when the fault is removed, so it's just a slow service."**
With the short queue, yes, and we call that fault-coupled. With the long queue it doesn't
disappear: success stays near 0% for the whole 25 s observation window after the fault is gone,
while order_service is 100% busy.

**"You injected the fault, so of course it fails."**
We inject a slowdown. The *failure to recover after the slowdown is removed* is not injected. It
comes entirely from retries plus queueing, and it disappears when retries are controlled. Same
fault, different retry policy, opposite outcome.

**"Your controller just turns retries off."**
Only where and while there's congestion. In healthy periods it keeps 3 attempts and 100% success.
In the error burst it keeps almost all of the retry benefit: 70% success, against 73% for static
retries and 40% for no retries.

**"Why trust 0 false alarms?"**
It's 636 healthy windows, including two control runs at 26–28 users/s, close to the testbed's
capacity, with no fault. It stays at 0 under every threshold setting in the sweep.

**"The model barely beats the rule overall."**
Overall, F1 is 0.75 against 0.60. On *onsets*, which is what pre-emption needs, it's 0.66
against 0.32, and it warns 4× earlier (8 s vs 2 s).
