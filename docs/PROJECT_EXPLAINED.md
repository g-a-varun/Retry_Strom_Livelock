# The whole project, explained from scratch

This is the "understand everything" file. It doesn't follow the slides. It follows the project itself: the background you need, what we built, how each part works inside the code, why every choice was made, what went wrong on the way, and what the results really mean.

`PRESENTATION_GUIDE.md` is the other file: how to present the deck, how to run the demo, and why each file exists. Read this one first.

**How to read it.** The chapters build on each other, so go in order the first time. It takes about 2-3 hours to read properly. Each chapter ends with a few "check yourself" questions. If you can answer them without looking, you understand that chapter. The answers are in the chapter text, and there's a bigger self-test at the end.

**Contents**

0. The project on one page
1. Background from first principles
2. The testbed: what we built and how it works inside
3. Why the storm happens: the mechanism, with numbers
4. The dataset: how it's made and why it can be trusted
5. Detecting livelock: the four conditions
6. The service graph and "message passing"
7. Predicting livelock: the model
8. Evaluating the model properly
9. The controller: how it decides
10. The experiments: what we compared and what we found
11. How the project evolved: what went wrong and what we fixed
12. Limitations: what's general and what's specific to our testbed
13. Code map: where everything is
14. Glossary
15. Self-test

---

## 0. The project on one page

**The question.** When a microservice slows down, every service that calls it retries. Can those retries push the whole system into a state where it's 100% busy but serves nobody, and stays there even after the slow service is fixed? If so, can we see it coming and stop it?

**What we built.**
1. A **testbed**: 4 real services (separate OS processes, real HTTP calls, real threads and semaphores) where we can make one service slow or error-prone on purpose.
2. A **detector**: a formal four-condition test that says, for each call edge and each 2-second window, "this is livelock" or "it isn't".
3. A **predictor**: a logistic-regression model that says, every 2 seconds, "this edge will be in livelock in the next 2-12 seconds" (or not).
4. A **controller**: a program that runs every second, uses the predictor, and cuts or restores the number of retries on each edge while the system runs.
5. A **dataset** (generated, because no public one has the data this needs) and a script that **validates** it.

**What we found.**
- Yes, it happens, and it can sustain itself. With the usual "3 attempts" retry policy, a 20-second slowdown turned into a failure that never ended: 2.9% of users served after the fault was removed, 0 of 5 runs recovered, and 98.2% of the busy service's work was for requests nobody was waiting for any more.
- The detector found 131 livelock windows with 0 false alarms in 636 healthy windows.
- The predictor warned before all 30 livelock episodes in the data, a median of 8 seconds early, with 94.1% accuracy and 80.7% precision on runs it never saw.
- With the controller on, the self-sustaining livelock never formed and the system recovered in all 5 runs, the fastest of all strategies. And when retries were actually useful (a burst of fast errors), it kept almost all their benefit (71% success vs 74% for plain retries).
- **The main lesson:** retries help when a service fails fast and hurt when it's saturated. No fixed retry policy is right in both cases, so the retry budget has to adapt while the system runs.

Keep this page in your head. Everything below is detail on one of these lines.

---

## 1. Background from first principles

### 1.1 Microservices and calls

A **microservice** is a small program that does one job and talks to other programs over the network. Our shop has four: a **gateway** (the front door users hit), an **order** service, a **payment** service and an **inventory** service.

When the gateway needs an order placed, it sends an **HTTP request** to the order service and waits for the **response**. The one sending is the **caller**; the one answering is the **callee**. A caller→callee link is a **call edge**. A user's request travels down a **call chain**:

```
user ──> gateway ──> order_service ──> payment_service
                                  └──> inventory_service
```

We have three edges: gateway→order (e1), order→payment (e2), order→inventory (e3). Most of the project is about looking at each **edge** separately, because a storm lives on an edge, not on a service.

HTTP status codes you'll see everywhere:

| Code | Meaning in our project |
|---|---|
| 200 | success |
| 500 | the service failed on its own (the injected or built-in error) |
| 502 | order_service failed because something *below* it failed |
| 503 | "busy": the service had no free worker slot in time |
| 0 | not a real HTTP code: we log 0 when the caller **timed out** (or couldn't connect) |

### 1.2 Timeouts: the caller gives up, the callee doesn't know

A caller can't wait forever, so it sets a **timeout**: "if there's no answer in 2 seconds, give up". Ours is 2 s per attempt.

The key subtlety: **when the caller gives up, the callee is not told.** The callee keeps working on that request, finishes it, and sends a reply into a closed connection. That finished work was for nobody. We call it **orphaned work**, and it's the centre of this whole project. (Real systems mostly behave this way too, because cancelling work across services is hard.)

### 1.3 Retries, backoff and jitter

Most failures are short (a blip, a lost packet, one slow request), so a second try usually works. That's why almost every real client **retries**: after a failure or timeout, try again, up to some limit. Ours: up to **3 attempts** per call.

Retrying immediately is a bad idea, so clients wait a little first: **backoff**. **Exponential** backoff doubles the wait each time: 100 ms before the 2nd attempt, 200 ms before the 3rd.

**Jitter** adds randomness. Without it, a thousand clients that failed at the same instant all retry at the same instant too, and hit the server in lock-step waves (a "thundering herd"). We use **equal jitter**: half the delay is fixed, half is random. So the real wait before attempt 2 is 50-100 ms, and before attempt 3 is 100-200 ms. In the code (`common/retry_client.py`):

```python
def backoff_with_jitter_ms(backoff_base_ms, attempt_number):
    full_delay = backoff_base_ms * (2 ** (attempt_number - 1))
    return full_delay / 2.0 + random.uniform(0, full_delay / 2.0)
```

**Why retries are good, with real numbers.** Our healthy services fail on their own: order 2% of the time, payment and inventory 5% each. With **no** retries, a checkout succeeds only if all three succeed: 0.98 × 0.95 × 0.95 ≈ **0.884**. That's exactly the **88.4%** we measured for the no-retry strategy in healthy traffic. With 3 attempts, payment fails only if all three attempts fail: 0.05³ = 0.000125, so success is essentially **100%**, which is also what we measured. That's why "just turn retries off" is not the answer.

**Why retries multiply.** The gateway tries order up to 3 times, and **each** order attempt tries payment up to 3 times. So one user click can become 3 × 3 = **9** calls to payment (and 9 to inventory). Each extra level multiplies again.

### 1.4 Capacity: threads, semaphores and queues

**Threads.** Each service is a Flask web server started with `threaded=True`, so every incoming request is handled by its own thread.

**Worker slots = a counting semaphore.** Real servers can only do a few things at once (a fixed thread pool, CPU cores, DB connections). We model that with a **counting semaphore**: a counter of permits. A request must `acquire()` a permit before doing its work and `release()` it after. Payment and inventory have **2** permits, order has **8**, the gateway has no limit. From `services/payment_service.py`:

```python
worker_slots = threading.Semaphore(args.slots)
...
got_slot = worker_slots.acquire(timeout=config["queue_wait_sec"])
if not got_slot:
    return "payment service busy", 503
try:
    should_fail, latency_ms = do_work(config)
finally:
    worker_slots.release()
```

**The queue is real.** When all permits are taken, new request threads block inside `acquire()`. Those blocked threads **are** the queue. Nothing is simulated: it's exactly how a thread pool's waiting queue behaves.

**Bounded waiting.** `acquire(timeout=queue_wait_sec)` means a request waits at most that long for a permit, then gives up with 503. This one number turns out to decide whether the storm can outlive the fault (Chapter 3):
- **1 s** (the default): requests that can't be served are rejected fast.
- **10 s** (the "long queue" runs): like a big thread-pool queue in a real server. A request can still be waiting long after its caller gave up at 2 s.

**Order holds its slot while it waits for payment and inventory.** Look at `services/order_service.py`: it acquires a slot, does its own 10-40 ms of work, then calls payment **and then** inventory (each with retries), and only then releases the slot. So order's slots are tied up for as long as payment and inventory take. When payment gets slow, order gets slow too, even though nothing is wrong with order itself.

### 1.5 The queueing maths you actually need

Three small formulas, all used in the project:

**Capacity.** A service with `c` slots where each request holds a slot for `S` seconds can finish at most **c / S requests per second**.
- Payment, healthy: S ≈ 60 ms on average (30-90 ms), so 2 / 0.06 ≈ 33 per second.
- Payment, during the fault (700-900 ms): 2 / 0.8 = **2.5 per second**. That's why everybody gets about 5% success during the fault: no retry policy can create capacity.

**Utilization Law.** Utilization U = X · S / c, where X is the throughput. It can never go above 1 (100% busy). Healthy payment at 20 calls/s: 20 × 0.06 / 2 = 0.6, so 60% busy. `validate_data.py` checks that no service is ever measured above 100%. If it were, our logs would be wrong.

**Little's Law.** The average number of requests in flight N = X × R, where R is the average time each one spends inside. You can see it in our data: during a storm on gateway→order, about 50 attempts per second each wait the full 2 s timeout, so N ≈ 50 × 2 = **100** calls in flight. The labelled data shows 100-105. Healthy: about 17 calls/s × 0.19 s ≈ **3.2**, and the healthy baseline we measured is 3.28.

**Arrivals faster than capacity = the queue grows forever.** If requests arrive at 60/s and you can finish 32/s, the queue grows by 28 every second until something gives (here: the 10 s wait limit or the user's 15 s timeout). If they arrive at 20/s and you finish 32/s, a backlog shrinks by 12 every second.

### 1.6 Deadlock vs livelock vs overload (the OS view)

| | Deadlock | Livelock | Plain overload |
|---|---|---|---|
| Threads | blocked, waiting for each other | all running | all running |
| CPU / slots | idle | 100% busy | 100% busy |
| Useful work | none | almost none | up to capacity |
| Cause | circular wait | failures create more work (positive feedback) | more demand than capacity |
| Ends when the trigger ends? | no | not always | yes |

**Livelock** is in the OS syllabus right next to deadlock: processes keep running and changing state, but none of them makes progress. The classic picture is two people in a corridor who keep stepping aside the same way. A close relative is **receive livelock** in old interrupt-driven kernels: under heavy network load, the kernel spends all its CPU taking in packets that it then has to drop, so no packet ever gets fully processed. Our order service does exactly that with orphaned requests.

The important difference from overload: an overloaded system still does useful work at full capacity. A livelocked system is equally busy, but the work is wasted.

### 1.7 Metastable failures

Bronson et al. (2021) gave the bad case a name: a **metastable failure**. Three ingredients:
1. The system is in a normal ("stable") state.
2. A **trigger** (here: payment gets slow for 20 s) pushes it into a bad state.
3. A **sustaining effect** (here: retries plus orphaned work) keeps it there **even after the trigger is gone**.

The system is **bistable**: the same healthy services, the same load, two possible states, and which one you're in depends on history. Huang et al. (OSDI 2022) studied 22 real metastable failures from 11 organisations; at least 4 of 15 major AWS outages in a decade were of this kind.

**An analogy.** A restaurant where waiters re-send an order to the kitchen if the food takes more than 2 minutes. When the cook slows down, the kitchen gets three copies of every order, cooks meals for tables that already left, and the tables still waiting get nothing, so their waiters re-send too. Even after the cook is back to full speed, the pile of duplicate orders keeps the kitchen busy forever. It only clears if someone tells the waiters to stop re-sending for a while. That "someone" is our controller.

### 1.8 Open-loop load and Poisson arrivals

**Closed loop:** each simulated user waits for its reply before sending the next request. If the system slows down, the load automatically slows down too, which hides the storm.

**Open loop:** users arrive at a fixed rate no matter what, like real users on a website, who don't know or care that the previous person is still waiting. We use open loop. Every arriving user gets its own thread (`scenario_runner.py`, `traffic_loop`).

**Poisson arrivals:** in real traffic, arrivals are random, not evenly spaced. If on average λ users arrive per second, the gaps between arrivals follow an exponential distribution with mean 1/λ. In the code: `next_time = next_time + rng.expovariate(rps)`. It's seeded, so a run can be repeated exactly.

### 1.9 The industry's usual fix: retry budgets

The standard advice (AWS Builders' Library, Google SRE book, gRPC retry throttling) is a **retry budget**: allow retries only up to some fraction of fresh calls, say 10%. It's implemented as a **token bucket**: every fresh call drops 0.1 token into the bucket, every retry takes 1 token out, and if the bucket is empty the retry is not sent. From `common/retry_client.py`:

```python
def budget_take(name):
    with budget_lock:
        tokens = budget_tokens.get(name, BUDGET_MAX_TOKENS)
        if tokens >= 1.0 - 1e-9:
            budget_tokens[name] = tokens - 1.0
            return True
        return False
```

A budget is safe in a storm, because retries can never be more than about 10% of the load. But it's **blind**: it throws away useful retries too. When a service is failing fast and has free capacity, a retry is a free second chance, and the budget still blocks it. That weakness is exactly what our error-burst experiment shows (Chapter 10).

### Check yourself (Chapter 1)

1. Why does a timeout create wasted work?
2. Where exactly is the "queue" in our services, in terms of threads?
3. Why do 3 attempts give about 100% healthy success when no retries give about 88%?
4. Payment has 2 slots and takes 0.8 s per request during the fault. What's its maximum throughput?
5. Use Little's Law: 50 attempts/s, each waiting 2 s. How many in flight?
6. Why would a closed-loop load generator hide the storm?

---

## 2. The testbed: what we built and how it works inside

### 2.1 The four services

| Service | Port | Slots | Own work (healthy) | Own errors | Max wait for a slot | Calls |
|---|---|---|---|---|---|---|
| gateway | 5000 | no limit | (none) | (none) | (none) | order, up to 3 attempts |
| order_service | 5003 | 8 | 10-40 ms | 2% | 1 s, or 10 s in long-queue runs | payment then inventory, up to 3 attempts each |
| payment_service | 5001 | 2 | 30-90 ms | 5% | 1 s | none |
| inventory_service | 5002 | 2 | 30-90 ms | 5% | 1 s | none |

Every retrying caller uses the same policy: up to 3 attempts, 2 s timeout per attempt, backoff 100 then 200 ms with equal jitter. A user gives up after 15 s.

Each service is a separate OS process started by `run_all.py`. They talk over real HTTP on 127.0.0.1. Each also has `/health` (used at start-up) and `/stats` (in-flight counters, used to wait until the system is idle between runs).

### 2.2 Follow one checkout request through the code

This is the single most useful thing to be able to explain. Take one user at time t.

1. **The load generator creates the user.** `scenario_runner.py`, `traffic_loop()`: at each Poisson arrival time it starts a new thread running `fire_one_request()`. That thread sends `GET /checkout` to the gateway with the scenario id, the current phase name, and a 15 s timeout. When the answer comes back (or 15 s pass), it writes one line to `user_requests.jsonl`: when it started, how long it took, and whether it succeeded.

2. **The gateway passes it on.** `services/gateway.py`, `checkout()`: no slot limit, it just calls `call_with_retry("gateway", "order_service", ...)`. If that returns success, the user gets 200 ("checkout complete"); otherwise 502. It then logs one line to `server_gateway.jsonl`.

3. **The retry client makes the attempts.** `common/retry_client.py`, `call_with_retry()`:
   - reads the **current** retry policy for this edge from `data/retry_policy.json` (on every call, so the controller's changes take effect at once);
   - makes a `call_id` shared by all attempts of this call;
   - for each attempt: sets `deadline = now + timeout` and sends it in an **`X-Deadline` header**, sends the request, records the response code (0 if it timed out);
   - decides whether to retry (failed, attempts left, and budget allows if there is one);
   - logs **one line per attempt** to `telemetry_gateway.jsonl` (attempt number, response code, latency, backoff, policy, call id);
   - sleeps the backoff and tries again, or returns.

4. **Order queues for a slot.** `services/order_service.py`, `checkout()`: reads its config, then `worker_slots.acquire(timeout=queue_wait_sec)`. If no slot comes free in time it logs a 503 and replies "busy".

5. **Order does its work while holding the slot.** `do_work()` sleeps 10-40 ms (or fails 2% of the time → 500). Then it calls payment with retries, then inventory with retries (each attempt logged to `telemetry_order_service.jsonl`). If either finally failed → 502. Then it **releases the slot**.

6. **Payment and inventory do the same, one level down.** Acquire one of 2 slots (1 s max wait, else 503), sleep 30-90 ms (or longer during a latency fault), fail 5% of the time (more during an error fault), release, reply.

7. **Every service logs what it received.** `log_server_request()` writes one line to `server_<service>.jsonl`: when it arrived, when it got a slot, its status, the caller's deadline, and two flags:
   ```python
   orphaned = done_time > deadline
   expired_in_queue = slot_time > deadline
   ```
   `orphaned = true` means the service finished after its caller had already given up: the work was for nobody.

**The important line**, in a comment in `order_service.py`: *"this service does NOT check whether the gateway is still waiting. Like most real services, it finishes the work anyway. That is where wasted (orphaned) work comes from."*

### 2.3 How a fault is injected without restarting anything

Each service has a small config file in `code/configs/`, for example `payment_service.json`:

```json
{"failure_rate": 0.05, "min_latency_ms": 30, "max_latency_ms": 90, "queue_wait_sec": 1.0}
```

`common/fault_injection.py`, `load_config()` reads that file **on every request** (and again after getting a slot, in case it changed while the request queued). `do_work()` then sleeps a random time between `min_latency_ms` and `max_latency_ms`, and fails with probability `failure_rate`.

So to make payment slow, `scenario_runner.py` just rewrites `payment_service.json`. The next request feels it. No restart, no special code path.

**The ramp.** A "gradual" fault rises in steps (5 steps over 10 s in the demo). At severity s (from 0 to 1), the latency range goes linearly from healthy (30-90 ms) to the peak (peak ± 100 ms):

```python
config["min_latency_ms"] = int(normal["min_latency_ms"] + (peak_min - normal["min_latency_ms"]) * severity)
```

With the default 800 ms peak, step 1 of 5 (s = 0.2) gives 164-252 ms and step 5 gives 700-900 ms. That's exactly what live1's output prints. An **error fault** ramps `failure_rate` instead (to 70% in the error-burst experiment).

**Healthy by default.** `run_all.py` rewrites every config to healthy every time the services start, and `scenario_runner.py` puts healthy configs back in a `finally:` block, even on Ctrl+C. So a crashed run can never leave a fault switched on for the next one.

### 2.4 How the controller changes retries without restarting anything

Same trick, other direction. The retry policy for every edge lives in `data/retry_policy.json`:

```json
{"gateway->order_service": {"max_attempts": 2, "backoff_base_ms": 200, "timeout_ms": 2000, "budget_ratio": null}, ...}
```

`common/retry_policy.py`, `get_retry_policy()` is called on every call. No file means everyone uses the default (3 attempts, 100 ms, 2 s). `clean_policy()` refuses nonsense values (attempts below 1, a negative backoff), whoever wrote the file.

**Two processes sharing a file safely** (`common/safe_files.py`):
- **Half-written file problem:** if a service reads while the controller is halfway through writing, it gets broken JSON. Fix: write to a temp file, then `os.replace()` it over the real one. A rename is **atomic**: a reader sees either the whole old file or the whole new one, never half.
- **Windows problem:** on Windows, `os.replace` fails if another process has the file open at that exact moment. Fix: retry up to 50 times, 10 ms apart.
- **Fallback:** if a read still fails, the service uses the **last good** policy it saw (`last_good_policy`), so a bad moment never crashes a request.

That's a small OS lesson in itself: atomic rename, file locking differences between Windows and Linux, and graceful fallback.

### 2.5 Telemetry: what gets logged, and why this way

All logs are **JSON Lines**: one JSON object per line, appended. Each process only ever appends to **its own** files, so two processes never write into the same file.

| File | Written by | One line per | Key fields |
|---|---|---|---|
| `telemetry_gateway.jsonl`, `telemetry_order_service.jsonl` | the caller | call **attempt** | attempt_number, response_code (0 = timeout), latency_ms, backoff_delay_ms, retry_policy, call_id |
| `server_<service>.jsonl` (4 files) | each service | request **received** | arrived, slot_time, status, deadline, orphaned, expired_in_queue, queue_wait_ms, service_ms |
| `user_requests.jsonl` | load generator | user checkout | start_time, succeeded, status_code, total_time_ms |
| `scenarios.jsonl` | scenario runner | run | all settings + exact start/end of every phase (the **ground truth**) |
| `controller_log.jsonl` | controller | edge per second | stats, risk, action, attempts allowed |

Why log at the **caller**? Because only the caller knows the attempt number and that it timed out. The callee never sees a timeout, it just finishes late. Why log at the **callee** too? Because only the callee knows when the work really finished, which is what tells us if it was orphaned. You need both sides.

Every record carries `scenario_id`, so one big file can hold many runs and each analysis picks out its own. `within_run()` also keeps only records between a run's start and end (plus margins), so an older, interrupted run with the same name can't leak in.

### 2.6 Running things: `run_all.py` and `scenario_runner.py`

`run_all.py`:
- checks ports 5000-5003 are free (otherwise an old copy is still running);
- resets every config to healthy and deletes any old retry policy;
- starts the four services as child processes (leaves first, gateway last) and waits until each answers `/health`;
- watches them; Ctrl+C stops all four;
- `wait_until_idle()`: polls every service's `/stats` until nothing has been in flight for 1 second. Used between runs, because with a 10 s queue, abandoned requests can keep a service busy for a while after the users have gone, and that leftover work must not leak into the next run.

`scenario_runner.py` runs **one** experiment:
- refuses a scenario id that was already used (so two runs never mix);
- waits until the system is idle;
- builds the schedule: normal → ramp steps → fault (held) → recovery. A "spike" skips the ramp; mode "none" is a control run with no fault;
- starts the open-loop traffic thread, then walks through the schedule, rewriting the faulty service's config at each step and printing what it set;
- after the traffic ends, waits up to 17 s for the last users to come back;
- writes the run's **manifest** to `scenarios.jsonl`: every setting and the exact start and end of every phase;
- on Ctrl+C: removes the fault, marks the run `"interrupted": true` (every analysis script skips interrupted runs), and exits.

`analyze_run.py <id>` reads one run back and prints, per phase: users, success %, goodput, latency, calls, retry %, calls per success, orphaned %, plus the success rate of users who arrived in the **last 5 seconds**. That last line is the cleanest "did it recover?" test, because the per-phase average includes the time spent draining.

### 2.7 Why these particular settings

| Setting | Why |
|---|---|
| Payment and inventory with 2 slots | makes them the bottleneck, so a slowdown there backs up into order |
| Order with 8 slots | the shared middle queue where gateway retries pile up |
| Order's slot wait 1 s or 10 s | 1 s gives the fault-coupled kind of storm, 10 s the self-sustaining kind |
| Gateway with no limit | like a load balancer / API gateway: accepts everything and forwards it |
| 5% built-in errors | real systems always have a trickle of failures, hence a trickle of retries |
| 2 s timeout, 3 attempts, 100 ms backoff | common real-world client defaults |
| 20 users/s | about 60% utilization on the leaves when healthy: comfortable, not idle |
| 15 s user timeout | a user waits a while, but not forever |

### Check yourself (Chapter 2)

1. Trace a checkout from the load generator to payment and back. Name the function at each step.
2. Why does order's slot stay busy when payment is slow?
3. How does `scenario_runner.py` slow payment down without restarting it?
4. What problem does `os.replace` solve, and what extra problem does Windows add?
5. Why do we log each call at both the caller and the callee?
6. What does `orphaned = done_time > deadline` mean in words?

---

## 3. Why the storm happens: the mechanism, with numbers

This chapter is the heart of the project. If you can explain this with numbers, the rest is easy.

### 3.1 Healthy

- 20 users/s arrive. Each needs one order call, and order makes one payment and one inventory call.
- Payment: 20 calls/s × 0.06 s ÷ 2 slots = **60% busy**. Same for inventory.
- Order holds a slot for about 0.2 s (its own work plus the payment and inventory calls; we measured a mean of 0.20 s in healthy traffic). Little's Law: 20 × 0.2 ≈ 4 slots busy out of 8, and order's capacity is about 8 ÷ 0.2 = 40 requests/s.
- Retries are rare (only the built-in 2-5% failures), ρ ≈ 0.03. Users get 100%.

### 3.2 During the fault (payment at 700-900 ms)

- Payment can finish at most 2 / 0.8 = **2.5 requests/s**. Users still arrive at 20/s.
- Order holds its slots while waiting for payment, so order's throughput is also capped at about 2.5/s. All 8 slots fill up and requests start queueing.
- Requests waiting in order's queue for more than 2 s are given up on by the gateway, which retries. Now up to 3 attempts per user arrive at order: up to **60/s** offered.
- Order's own payment calls also time out and retry, which adds more load on payment.
- Success during the fault is about **5-6% for every strategy** in our experiments. That's the capacity limit, and no retry policy can change it. The interesting question is what happens **after** the fault.

### 3.3 Orphans pile up (with a 10 s queue)

With a 10 s slot wait, a request can sit in order's queue for up to 10 s. But the gateway gave up on it after 2 s. When it finally gets a slot, order does all the work (including calling payment) and replies to nobody. Meanwhile it held a slot that a live request needed, so that live request also waited too long, timed out, and became an orphan in turn.

This is the **sequence diagram in the report** ("How a retry creates orphaned work"): attempt 1 waits → gateway times out at 2 s → attempt 2 is sent → attempt 1 finally runs, for nobody.

### 3.4 After the fault: the capacity inequality

Payment is healthy again. Does the system come back? Just after the fault, order can finish about **μ ≈ 8 slots ÷ ~0.25 s ≈ 32 requests/s**. (0.25 s rather than the healthy 0.2 s, because payment and inventory are still busy with the queued work.)

Now the two possible states:

**The stuck state.** There's a backlog of about 200 requests in order's queue. Draining it at 32/s takes about 6 s, which is more than the gateway's 2 s timeout. So almost every request that gets a slot has already been abandoned: the work is orphaned, the user gets nothing, and the gateway sends another attempt. With 3 attempts, up to **3 × 20 = 60 requests/s** arrive. 60 > 32, so the backlog never shrinks. The state sustains itself. This is what we saw: 2.9% success after the fault, 98.2% of order's work orphaned, 0 of 5 runs recovered.

**The good state.** If the queue is short, waits are under 2 s, most attempts succeed first time, so arrivals are about λ = 20/s. 20 < 32, so it stays short.

Same services, same load, two stable states. That's **bistable**, and the retry policy decides which one you end up in.

**The exit condition** (the one line to remember):

> arrival rate × retry amplification < capacity

Cut retries to 1 attempt: 20 × 1 = 20 < 32, so the backlog drains at 32 − 20 = 12 per second. A 200-request backlog takes about 200 ÷ 12 ≈ 16 s. We measured **12.0 s** for the no-retry strategy, in the same ballpark (the real backlog and capacity vary a little).

### 3.5 Why the queue length decides everything

With a **1 s** slot wait, a request that can't get a slot is rejected with 503 after 1 s, **before** the gateway's 2 s timeout. The gateway gets a fast "busy", not a timeout, and nothing stale is left in the queue to be processed later. The storm is severe while the fault lasts, but once the fault is gone there's no backlog of orphans, so it disappears in a few seconds. We call that **fault-coupled** livelock.

With a **10 s** slot wait, stale requests stay in the queue and get processed later for nobody. That's what makes the storm **self-sustaining** (metastable).

This is also why "drop work nobody is waiting for" (deadline-aware queues) is a known fix in industry. We list it as future work; our project attacks the other half of the loop, the retries.

### 3.6 Why acting early beats acting late

If you cut retries only after the storm has formed (the reactive controller, at 17.5-19.5 s), a backlog is already there and has to drain. If you cut while the slowdown is just starting (the predictive controller, at 11.5 s, 1.5 s into the ramp), fewer extra attempts ever enter the queue. Result in our experiments: reactive let livelock form in all 5 runs (12 windows) and did 974 calls during the fault; predictive never let it form (0 windows) and did 661 calls.

### 3.7 Where the storm shows up: on gateway→order, not on payment

You'd expect the faulty service's edge (order→payment) to be the red one. It isn't. Order has only 8 slots, so at most 8 requests at a time can be calling payment, which caps how many retries can pile up towards payment; its ρ stays around 1. The gateway has no limit, so its retries to order pile up freely: ρ ≈ 2, with 100+ calls in flight. The detector points at **where the storm is**, which is not the same as **where the fault is**. That's the whole reason to analyse per edge.

### Check yourself (Chapter 3)

1. Why is success about 5% during the fault for every strategy?
2. State the capacity inequality with our numbers, for 3 attempts and for 1 attempt.
3. Why is a backlog of about 200 enough to keep the system stuck? (Hint: compare the drain time with the timeout.)
4. Why doesn't a 1 s queue give a self-sustaining storm?
5. Why does the storm show up on gateway→order instead of order→payment?

---
## 4. The dataset: how it's made and why it can be trusted

### 4.1 Why we couldn't download one

To label retry livelock, every call record needs three things:
1. **which attempt** it was (1st, 2nd, 3rd), or you can't count retries;
2. its **outcome** (success, error, timeout), or you can't tell useful work from wasted work;
3. **when the fault started and ended**, or you can't say whether the system recovered.

| Dataset | Attempt number | Outcome | Fault timing |
|---|---|---|---|
| Alibaba microservice traces (2021, 2022) | no | no (response time only) | no |
| Meta traces (ATC 2023) | no | no | no |
| Uber "Tale of Errors" (SIGMETRICS 2025) | no | partly (error tags) | no |
| RCAEval (2024) | no | partly | yes |
| OSDI 2022 metastability work | yes | yes | yes, but it's a **method**, not a dataset: they ran a system and injected a trigger |

So we do what the OSDI 2022 paper did: run a real system, inject a trigger, log every attempt. (`calibrate_from_trace.py` can read the Alibaba trace, but that trace can only tell you call-graph shape and response times. It's optional and not used for any result. `docs/DATASET_SEARCH.md` has the full search.)

### 4.2 What `generate_dataset.py` does

It draws **24 random scenarios** from a seeded random generator (seed 7), so the same command always gives the same list. Six factors are varied:

| Factor | Values | Why vary it |
|---|---|---|
| fault kind | latency (60%) or error (40%) | slow and failing services behave very differently |
| target | payment (60%) or inventory (40%) | the storm must not depend on which leaf is faulty |
| order's slot wait | 1 s (50%) or 4, 6, 8, 10 s | short queues give fault-coupled storms, long ones self-sustaining |
| load | 12-22 users/s | light to heavy |
| fault shape | gradual ramp (80%, 3-6 steps over 6-14 s) or sudden spike (20%) | ramps give the model something to see coming |
| severity | peak latency 500-900 ms, or peak error rate 60-90% | mild to harsh |

Plus **4 control runs with no fault at all**, at 16, 18, **26** and **28** users/s (slot wait 1 s or 8 s). The two high-load ones are there to test that the detector doesn't call a merely busy system "livelock".

Every dataset run uses the **static 3-attempt policy** with no controller, on purpose: the model has to learn what a storm looks like when nothing stops it.

Each run is normal traffic (10-12 s, which gives the healthy baseline) → ramp or spike → fault held (6-12 s) → recovery (15-20 s). The 28 runs took 22.9 minutes.

With the 50 experiment runs (Chapter 10), `data/` holds **78 runs**: 264,223 call attempts, 71,621 user requests and 335,844 server records. Every live run you do (live1, demo1, demo2, ...) is added to the same logs, so `validate_data.py` counts grow by a few runs; the checks still pass.

### 4.3 Why a generated dataset is valid here

1. **Precedent.** The paper that established this failure class generated its data the same way.
2. **Only the fault is injected.** We change one service's latency or error rate. We never script a retry, a queue, a timeout or a collapse. Everything after the fault is the system's own behaviour, coming from real threads, semaphores, sockets and timeouts.
3. **It's a real system, not a simulator.** 4 OS processes, real HTTP.
4. **Exact ground truth.** The manifest records the exact second each phase started and ended. No production trace gives you that.
5. **Variety and controls.** 24 random scenarios over 6 factors, plus 4 no-fault controls.
6. **It can be checked** (next section), and it's **reproducible**: regenerating the whole dataset three times gave 132, 135 and 131 livelock windows.

What we give up: the absolute numbers (capacities, latencies) belong to this testbed. The mechanisms (queueing, amplification, orphans) are general.

### 4.4 How `validate_data.py` checks it: 6 groups, all pass

The idea: there are things that **must** be true if the testbed behaves like a real system and the logs are honest. If any failed, we'd know the data was broken.

1. **Bookkeeping: nothing lost, nothing invented.** Every user request the load generator sent is in the user log (largest gap: 0). The number of requests each service received equals the number of first attempts its caller logged (largest gap: 0).
2. **Retry rules.** Over 218,897 logical calls: attempts are always numbered 1, 2, 3 in order with none missing, no call ever made more attempts than its policy allowed, and no call was ever retried after it had succeeded.
3. **Queueing theory.** Using the exact busy time of every request: no service is ever more than 100% busy in any 2 s window (Utilization Law, U ≤ 1). During a held latency fault, the slow service's measured throughput is about **91% of its theoretical maximum c / S**. So it really is saturated: busy, not idle.
4. **Fault injection did what it was told.** For latency faults, the measured service time matches the configured one (average bias +0.4%, from Python timer and logging overhead). For error faults, the measured failure fraction matches the configured rate. Healthy leaves fail 5.0% of the time on average (configured: 5%).
5. **Load generator.** The achieved arrival rate is within 2.5% of the target on average (worst 8.7%, which is normal for random Poisson arrivals over a short run).
6. **Timestamps.** 670,970 records checked; none ends before it starts.

### Check yourself (Chapter 4)

1. Which three fields does livelock labelling need that public traces don't have?
2. Why are all dataset runs recorded with static retries and no controller?
3. What are the two high-load control runs for?
4. What does the Utilization Law check prove about our logs?
5. What would it mean if "requests received ≠ first attempts logged"?

---

## 5. Detecting livelock: the four conditions (`labeling.py`)

### 5.1 Why windows, and why per edge

A single call can't be "livelocked"; livelock is a pattern over time. So we cut time into **2-second windows**, separately for each **edge**. 2 s is long enough to contain several calls per edge (otherwise the numbers are noise) and short enough to react in time.

Per edge because, as Chapter 3 showed, the storm lives on one edge (gateway→order) while the fault is on another (payment).

### 5.2 Turning attempt logs into numbers per window (`features.py`)

For each edge and window, `window_stats_for_edge()` and `finish_window()` compute:

| Symbol | Name in code | How it's computed | Meaning |
|---|---|---|---|
| λ_base | `lambda_base` | fresh calls (attempt 1) that **ended** in the window ÷ 2 s | new work per second |
| λ_retry | `lambda_retry` | retries (attempt ≥ 2) that ended in the window ÷ 2 s | repeat work per second |
| ρ | `rho` | retries ÷ fresh calls, capped at 10 | **retry amplification** |
| S | `s_useful` | successful attempts ÷ 2 s | **useful throughput** |
| N | `concurrency` | total time calls spent in flight inside the window ÷ 2 s | **average calls in flight** |
| P | `p_success` | successful ÷ all attempts | success fraction |
| | `fail_ratio` | failed **fresh** calls ÷ fresh calls | is the callee healthy? |
| | `overload_ratio` | (503s + timeouts) ÷ all attempts | is the callee **full**? |
| | `mean_latency_ms` | average attempt latency | how slow |

Three details worth knowing:
- **A call counts in the window where it ended**, because that's when its outcome is known.
- **N is a time-average, computed exactly.** A call that was in flight from 3.5 s to 5.0 s adds 0.5 s to window [2, 4) and 1.0 s to window [4, 6). Sum the in-flight time, divide by 2 s, and you get the average number in flight. (That's Little's Law territory: N ≈ throughput × time in system.)
- **`fail_ratio` looks at fresh calls only**, on purpose. If retries are switched off, ρ is 0 by definition, but the fresh-call failure ratio still honestly shows whether the callee is healthy. The controller depends on this.

### 5.3 Each edge is compared with its own healthy self

Edges have very different normal levels: a healthy gateway→order call takes about 190 ms with about 3.3 calls in flight, a healthy leaf call about 70 ms with about 1.3 in flight. So "slow" or "piling up" must mean slow or piling up **for that edge**. `edge_baseline()` takes the **median** over the first 8 s of the same run (4 windows, only those with at least 4 calls): S\* (healthy useful throughput) and N\* (healthy calls in flight). The median, not the mean, so one odd window can't distort it.

### 5.4 The four conditions

From `labeling.py`, `check_conditions()` (simplified only by removing the variable plumbing):

```python
cond1 = rho > 1.0                                          # C1
cond2 = s_useful < 0.5 * baseline_s_useful                 # C2
cond3 = concurrency > max(2.0 * baseline_concurrency, 1.0) \
        and concurrency >= 0.8 * concurrency_4s_ago        # C3
cond4 = lambda_retry >= 0.8 * lambda_retry_4s_ago \
        and p_success <= p_success_4s_ago + 0.05 \
        and lambda_retry > 0                               # C4
```

| | Name | Plain meaning | Why it's needed |
|---|---|---|---|
| C1 | retry amplification | more retries than fresh calls | the feedback loop is running. Without it, it's plain overload, not livelock |
| C2 | throughput collapse | useful work below half of healthy | no progress |
| C3 | not draining | lots of calls in flight (more than double healthy) and not falling | work is stuck inside the system |
| C4 | no damping | retries not falling and success not improving, compared with 4 s ago | the loop isn't dying out on its own |

**Livelock(e, w) = C1 and C2 and C3 and C4, holding for 2 windows in a row**, with at least 4 calls in the window.

Why each detail:
- **All four, not just ρ.** C4 alone is often true on healthy traffic (a steady trickle of retries is "not falling"). ρ alone can't tell a storm from a short burst of retries that are actually working. C1 + C4 separate livelock from overload; C2 + C3 say it's stuck.
- **The 20% tolerance** (`TREND_TOL = 0.2`) in C3 and C4. In a steady storm the counts wobble up and down at random. A strict "not falling" test (no tolerance, our Review-2 version) is then a coin flip each window, and most storm windows get missed. With 20%, only a **clear** improvement counts as recovering.
- **`P_TOL = 0.05`**: success must improve by more than 5 points to count as "improving".
- **Compared with 4 s ago** (`TREND_LOOKBACK = 2` windows), not the previous window: one window of noise can't flip it.
- **2 windows in a row** (`TAU_LOCK = 2`): removes one-off blips.
- **At least 4 calls**: too few calls in a window, nothing to judge.
- The thresholds are round numbers fixed **before** looking at results, then stress-tested (5.6).

### 5.5 A worked example: run ds_06, edge gateway→order

ds_06: gradual latency fault on **inventory**, 17 users/s, order slot wait 8 s. Phases: normal 0-12 s, ramp 12-25 s, fault 25-31 s, recovery 31-46 s.

**Baseline** (median of the windows at 0, 2, 4, 6 s): S\* = 15.25 calls/s, N\* = 3.09. So C2 fires below 0.5 × 15.25 = **7.6**, and C3 needs N above max(2 × 3.09, 1) = **6.2**.

| t (s) | phase | ρ | S | N | P | C1 | C2 | C3 | C4 | label |
|---|---|---|---|---|---|---|---|---|---|---|
| 8 | normal | 0.03 | 15.5 | 2.7 | 0.97 | - | - | - | **yes** | normal |
| 12 | ramp | 0.04 | 12.5 | 12.8 | 0.96 | - | - | yes | yes | normal |
| 14 | ramp | 0.00 | 6.5 | 29.3 | 0.59 | - | yes | yes | - | normal |
| 16 | ramp | 0.26 | 0.0 | 57.5 | 0.00 | - | yes | yes | yes | normal |
| 18 | ramp | 1.44 | 0.0 | 87.0 | 0.00 | yes | yes | yes | yes | normal (1st in a row) |
| 20 | ramp | 1.58 | 0.0 | 103.6 | 0.00 | yes | yes | yes | yes | **livelock** (2nd in a row) |
| 22-30 | ramp, fault | 1.8-2.1 | 0.0 | ~102-105 | 0.00 | yes | yes | yes | yes | livelock |
| 32-44 | **recovery** | 1.7-2.4 | 0-1 | ~102-114 | ~0.01 | yes | yes | yes | yes | **livelock** |

What to notice:
- **t = 8:** C4 is true on perfectly healthy traffic (retries went from 0 to 0.5/s, "not falling"). That's why C4 alone is useless.
- **t = 16:** three conditions hold, but ρ is only 0.26. Useful work has collapsed, but retries haven't overtaken fresh calls yet. Not livelock yet.
- **t = 18:** all four hold for the first time. **t = 20:** second window in a row, so it's labelled livelock.
- **N ≈ 100 calls in flight.** Little's Law check: λ_base + λ_retry ≈ 17.5 + 36 = 53.5 attempts/s, each timing out after 2 s: 53.5 × 2 ≈ 107. ✓
- **Recovery (t ≥ 32):** inventory has been healthy since 31 s, and the edge is still in livelock until the run ends. **Self-sustaining.**
- And the model's risk crossed its threshold (0.65) at **t = 12** (risk 0.97, while ρ was 0.04). First livelock window at t = 20: **8 s of warning** (Chapter 7).

### 5.6 How we know the labels mean something

There's no external "ground truth" for livelock anywhere, so it has to be defined. We check the definition three ways:

1. **It matches what users felt.** Users who arrived during a window flagged livelock got a success **9.0%** of the time, against **68.1%** for users in other fault or recovery windows. So the label picks out the windows where the system really was useless, not just "bad".
2. **It never fires on healthy traffic:** **0** false alarms in 636 normal-phase windows, including the 26 and 28 users/s controls (and 0 in 750 healthy windows of the experiment runs). With no retries, it can't fire at all (C1 needs retries), so the no-retry strategy gets 0, as it should.
3. **It doesn't hinge on one lucky threshold.** The sensitivity sweep (`labeling.py --sensitivity`) moved each threshold up and down and re-labelled everything:

| Threshold | Values tried | Livelock windows | False alarms |
|---|---|---|---|
| ρ limit (C1) | 0.5 → 1.5 | 156 → 97 | 0 at every value |
| β (C2) | 0.3 → 0.7 | 123 → 139 | 0 |
| C factor (C3) | 1.5 → 4 | 128 → 118 | 0 |
| trend tolerance | 0 → 0.3 | **45** → 153 | 0 |
| windows in a row | 1 → 4 | 183 → 66 | 0 |

Counts change smoothly (no cliff), false alarms stay at 0 everywhere, and the one parameter that matters a lot is the trend tolerance. With 0 tolerance only 45 of 128 storm windows are found, which is exactly the Review-2 bug.

**Overall on the dataset:** 131 livelock windows out of 1,878. Almost all on gateway→order (8 in the ramp, 46 in the fault, 63 in recovery), 1 on order→payment, 10 on order→inventory, and 3 just after the traffic ended.

### Check yourself (Chapter 5)

1. Why is `fail_ratio` computed on fresh calls only?
2. Why compare each edge with its own first 8 s, and why the median?
3. In ds_06 at t = 16, three conditions hold. Why isn't it livelock?
4. What goes wrong if the trend tolerance is 0?
5. Give the three pieces of evidence that the labels are meaningful.

---

## 6. The service graph and "message passing" (`graph_builder.py`, `features.py`)

### 6.1 The graph

For every 2 s window we have a small directed graph: **nodes** are the 4 services, **edges** are the 3 caller→callee links, and each edge carries that window's numbers (ρ, S, N, label, risk). Over time it's a **time-varying graph**: one snapshot per window. `graph_builder.py` builds the snapshots and draws `graph_snapshots.png`; the report redraws the same three snapshots with the values it prints (report Fig. 7).

### 6.2 Message passing, done by hand

The core idea of a graph neural network is that each part of the graph **learns from its neighbours**: it collects ("passes") information from nearby nodes or edges. We do one round of that by hand in `make_features()`. Each edge gets:

- **from the edges directly below it** (its children): the **worst** overload ratio, latency ratio and fail ratio among them (`down_max_overload`, `down_max_latency_ratio`, `down_max_fail_ratio`);
- **from the edge directly above it** (its parent): its ρ and overload ratio (`up_rho`, `up_overload_ratio`).

gateway→order has two children (order→payment, order→inventory) and no parent. Each leaf edge has gateway→order as parent and no children. Why "worst" of the children: one slow child is enough to back everything up.

### 6.3 Why it helps

A slowdown starts at the **bottom** (a leaf gets slow), but the storm forms at the **top** (gateway→order). So the first sign of trouble for gateway→order is latency rising on the edges **below** it, before anything changes on gateway→order itself. The graph features carry that sign upwards.

In ds_06 at t = 12 s, gateway→order already has risk 0.97 while its own ρ is 0.04. A rule watching ρ on that edge sees nothing. The model sees the latency rising below.

The evidence: without the graph features, PR-AUC **before onset** drops from **0.74 to 0.63**, and the bootstrap interval for that gain (+0.02 to +0.19) stays above zero, so the gain is real (Chapter 8).

### 6.4 Why not a real graph neural network?

We proposed a temporal GNN at the start of the project. With 1,458 training examples and a 4-node graph, a deep model would overfit and couldn't be computed by hand every second. One round of hand-written message passing captures the same idea, is explainable, and its benefit is measured by an ablation. A temporal graph network (TGN) is the natural next step, and the random forest's better score shows there's room for a stronger model.

### Check yourself (Chapter 6)

1. What exactly does gateway→order "receive" from its neighbours?
2. Why does information from below help predict a storm at the top?
3. How do we know the graph features help, rather than just hoping?

---

## 7. Predicting livelock: the model (`prediction_model.py`)

### 7.1 The question and the label

Every 2 seconds, for every edge: **"will this edge be in livelock at some point in the next 2-12 seconds?"**

Concretely, for window i, the label is 1 if **any** of the windows i+1 to i+5 is labelled livelock on that edge (`GAP_WINDOWS = 1`, `HORIZON_WINDOWS = 5`). Window i covers [2i, 2i+2); windows i+1..i+5 cover 2 to 12 seconds after window i starts. The current window is deliberately **not** part of the label. Otherwise the model could score well just by noticing that a livelock is already happening, which is detection, not prediction.

### 7.2 The examples

`build_examples()` makes one example per edge per window, skipping windows with no calls, windows after the run ended, and windows too close to the end to have a full 5-window future. Result: **1,458 examples from 28 runs, 216 positive (14.8%)**.

### 7.3 The 18 features

| Group | Feature | Meaning |
|---|---|---|
| Own edge (6) | `rho` | retries ÷ fresh calls now |
| | `fail_ratio` | fraction of fresh calls failing |
| | `overload_ratio` | fraction of attempts that got 503 or timed out |
| | `log_latency_ratio` | log of (mean latency ÷ this edge's healthy latency) |
| | `log_concurrency_ratio` | log of (calls in flight ÷ healthy calls in flight) |
| | `useful_ratio` | successful calls/s ÷ healthy successful calls/s (C2's quantity, capped at 5) |
| Temporal (4) | `d_rho`, `d_fail_ratio`, `d_latency_ratio`, `d_concurrency_ratio` | change since the previous window: is it getting worse? |
| Graph (5) | `down_max_overload`, `log_down_max_latency_ratio`, `down_max_fail_ratio` | worst values among the edges below |
| | `up_rho`, `up_overload_ratio` | values of the edge above |
| Edge id (3) | `edge_gateway_order`, `edge_order_payment`, `edge_order_inventory` | one-hot: which edge this is, because each has its own normal level |

**Why logs of the ratios?** Latency and calls-in-flight ratios are heavy-tailed: healthy is about 1, a storm is 10-50. A linear model on the raw ratio lets one huge value dominate; the log turns "×2, ×4, ×8" into equal steps (+0.69 each), which is how the risk should grow. A floor of 0.01 stops log(0).

**Why `useful_ratio` and `d_concurrency_ratio`?** They are the model's view of C2 (useful work collapsing) and of C3's trend (work piling up). The first version of the model had neither, so it saw "lots of errors" but not "no useful work", and raised false alarms in fast error bursts where users were still being served. Adding them is what moved precision from 68% to 81% (Chapter 8).

The healthy levels used for the ratios come from `data/baselines.json`: one value per edge, the median of every dataset run's warm-up baseline. `features.py` is the **only** place this maths lives. `labeling.py`, `prediction_model.py` and `controller.py` all call it, so the model is trained on exactly the numbers the controller computes live.

### 7.4 Logistic regression from first principles

**Step 1, standardise.** Features have very different scales, so each one becomes "how many standard deviations from its average": x′ = (x − mean) ÷ std. The means and standard deviations are saved in `model.json`.

**Step 2, a weighted sum.** z = b + w₁x′₁ + w₂x′₂ + … + w₁₈x′₁₈. A positive weight pushes towards "livelock coming", a negative one away from it.

**Step 3, squash into 0..1.** The **sigmoid**: p = 1 ÷ (1 + e^(−z)). z = 0 gives 0.5; a large positive z gives nearly 1; a large negative z gives nearly 0. That p is the **risk**.

**Training** chooses w and b to minimise the **log loss**, which punishes confident wrong answers heavily: for a true livelock (y = 1) the loss is −log(p), so p = 0.01 costs a lot. scikit-learn does this (`LogisticRegression(class_weight="balanced", C=1.0)`); C = 1 adds mild regularisation, which keeps weights from growing huge.

**Class balancing.** Only 15% of examples are positive. `class_weight="balanced"` makes each positive count 1458 ÷ (2 × 216) ≈ **3.4** times, and each negative 1458 ÷ (2 × 1242) ≈ **0.59** times, so both classes matter equally. Side effect: the risk values come out higher than true frequencies, which is why the decision threshold is **not** fixed at 0.5 but chosen from the training data (7.10).

**Why logistic regression and not something bigger?**
- The controller runs it every second for every edge: it must be fast (about 3 µs) and simple.
- It's **56 numbers** (18 means, 18 stds, 18 weights, 1 intercept, 1 threshold) in a 2.4 KB JSON file. No library is needed at run time.
- It's explainable: you can read the weights.
- A random forest (100 trees) is no better on F1 (0.79 vs 0.80), about 1,900× slower per prediction and about 0.5 MB (Chapter 8.8).

**The hand-written version** the controller uses (`features.py`, `risk_probability()`):

```python
z = model["intercept"]
for i, name in enumerate(model["feature_names"]):   # simplified: the real code also guards std = 0 and overflow
    z = z + model["weights"][i] * (values[name] - model["mean"][i]) / model["std"][i]
return 1.0 / (1.0 + math.exp(-z))
```

`prediction_model.py` checks this against scikit-learn on all 1,458 examples: the largest difference is about **5.6 × 10⁻¹⁶**, which is floating-point rounding. So the controller computes exactly what was trained.

### 7.5 Reading the weights (and why not to over-read them)

The biggest weights in the final model: `log_concurrency_ratio` +1.93, `useful_ratio` −1.78, `log_latency_ratio` −1.39, `overload_ratio` −1.21, `log_down_max_latency_ratio` +0.94, `rho` +0.80, `edge_gateway_order` +0.78.

- Calls piling up (+) and useful work falling (− on `useful_ratio`) come before a storm: that is C3 and C2 seen early.
- `log_down_max_latency_ratio` positive is the graph effect from Chapter 6: the edge below slows down first.
- `edge_gateway_order` positive: that edge is where storms form.
- `log_latency_ratio` and `overload_ratio` **negative** look backwards. They aren't bugs: the features are correlated, so a weight depends on what the other features already explain. With logistic regression you judge a **group** of features by removing it (an ablation), not by the sign of one weight.

### 7.6 Grouped cross-validation: no peeking

**The leakage trap.** Neighbouring windows of the same run are almost copies of each other. If you shuffle all windows and split 80/20, the test set is full of near-copies of training windows, and the score looks great for the wrong reason.

**What we do.** `GroupKFold(n_splits=5)` with the **scenario id as the group**: all windows of one run go into the same fold. 28 runs → 5 folds of 5-6 runs. Train on 4 folds, test on the 5th, rotate. Every example ends up with an **out-of-fold** risk from a model that never saw any window of its run. Those scores are saved in `results/prediction_oof.csv`, and every metric in Chapter 8 is computed from them. The standardiser is fitted on the training folds only.

### 7.7 What we compare against, and what each comparison tells us

| Comparison | What it is | What it tells us |
|---|---|---|
| **reactive rule** | alarm if ρ > 1 and overload > 20% **right now** (the reactive controller's rule) | does learning beat the obvious rule? |
| **persistence rule** | "it'll be livelock soon if it's livelock now" | how much is just "storms last a while"? |
| **without graph features** | same model, 13 features | does the graph help? |
| **without temporal features** | same model, 14 features | do the changes help? |
| **random forest** | 100 trees, depth 6 | is there headroom for a stronger model? |
| **shuffled-label control** | same model, trained on randomly shuffled labels | does the setup leak the answer? It should score at chance, and it does |

Every learned method gets its threshold the same way (7.10), so the comparison is fair.

### 7.8 The honest test: before onset

Most positive examples come from windows where the storm has **already started**. Predicting "still livelock in 2 s" when it's livelock now is easy. The real value of prediction is the windows **before** a storm begins, so we also score only the windows where the edge is **not** in livelock now and wasn't in the previous window: **1,341 examples, 123 positive**.

| Method | F1 all | F1 before onset | PR-AUC before onset |
|---|---|---|---|
| our model | **0.80** | **0.72** | **0.78** |
| without graph | 0.76 | 0.64 | 0.70 |
| without temporal | 0.80 | 0.71 | 0.78 |
| random forest | 0.79 | 0.69 | 0.79 |
| reactive rule | 0.60 | 0.32 | - |
| persistence rule | 0.53 | **0.00** | - |
| shuffled control | 0.22 | 0.15 | 0.07 |

Persistence gets 0 before onset (it can't see a storm that hasn't started), the reactive rule is weak (0.32), our model is clearly better (0.72). The graph features matter most here (+0.08 F1, +0.08 PR-AUC). The temporal features add very little, and we say so.

### 7.9 Lead time: how early is the warning?

An **episode** = consecutive livelock windows on one edge of one run; its **onset** is the first one. There are **30** episodes. For each, `lead_times()` looks at the 5 windows before the onset and finds the earliest alarm. Lead time = (onset − earliest alarm) × 2 s.

- Our model warned before **30 of 30** episodes, **median 8 s** ahead (range 2-10 s).
- The reactive rule warned before 24 of 30, median 2 s.
- Our model raised **0 alarms in 576** healthy windows (the first version raised 8).

8 s matters because the controller acts every 1 s and cuts at most every 2 s: 8 s is time for several small cuts before the storm forms.

### 7.10 Choosing the decision threshold without cheating

The risk is a number between 0 and 1; the alarm is "risk ≥ threshold". Picking the threshold that looks best on the data you report on would inflate the score. So the threshold is chosen **inside** the training data only (**nested cross-validation**):

1. In each outer fold, the 4 training folds are split again into 4 inner folds by run.
2. The inner folds give out-of-fold risks for the training runs only.
3. On those, `best_threshold()` tries 0.30, 0.35, …, 0.90 and keeps the one with the best F1.
4. The model trained on the 4 training folds is applied to the test fold with **that** threshold.

The test runs never influence their own threshold. The five folds chose 0.70, 0.80, 0.75, 0.70 and 0.65. The final model (trained on all 28 runs) gets its threshold the same way from a 5-fold run: **0.65**, saved in `model.json`, and the controller uses it.

### 7.11 What gets saved

`results/model.json`: feature names, means, stds, weights, intercept, threshold 0.65, baselines, the horizon. That's what the controller loads. `results/prediction_oof.csv`: every window's out-of-fold risk, its threshold and its alarm, for every method.

### Check yourself (Chapter 7)

1. Why isn't the current window part of the label?
2. Write the three steps from features to risk.
3. Why are three of the ratios fed in as logarithms?
4. Why would a random 80/20 split of windows give a misleadingly good score?
5. Why is "before onset" the honest test, and why does persistence score 0 there?
6. How is the threshold chosen without looking at the test runs?

---

## 8. Evaluating the model properly (`evaluate_model.py`, `show_metrics.py`)

Everything here uses the **out-of-fold** scores, so every window was judged by a model that never saw its run, with a threshold chosen without its run (7.10).

### 8.1 The confusion matrix

| | predicted "no" | predicted "yes" |
|---|---|---|
| **actually no** (1,242) | TN = **1,201** | FP = **41** |
| **actually yes** (216) | FN = **45** | TP = **171** |

### 8.2 Every metric, with our numbers

| Metric | Formula | Our numbers | Value | What it says |
|---|---|---|---|---|
| Accuracy | (TP + TN) / all | (171 + 1201) / 1458 | **0.941** | always saying "no" would score 1242 / 1458 = 0.852, so read it with the others |
| Precision | TP / (TP + FP) | 171 / 212 | **0.807** | of our alarms, 81% were right |
| Recall (sensitivity) | TP / (TP + FN) | 171 / 216 | **0.792** | we caught 79% of coming livelock windows |
| Specificity | TN / (TN + FP) | 1201 / 1242 | **0.967** | 97% of quiet windows stayed quiet |
| False-positive rate | FP / (FP + TN) | 41 / 1242 | 0.033 | 1 − specificity |
| Balanced accuracy | (recall + specificity) / 2 | (0.792 + 0.967) / 2 | **0.879** | not fooled by the rare class (always-"no" gets 0.5) |
| F1 | 2PR / (P + R) | 2 × 0.807 × 0.792 / 1.598 | **0.799** | one number for the precision/recall trade-off |
| F2 | 5PR / (4P + R) | | 0.795 | like F1 but recall counts double |
| MCC | (TP·TN − FP·FN) / √((TP+FP)(TP+FN)(TN+FP)(TN+FN)) | (171·1201 − 41·45) / √(212·216·1242·1246) | **0.765** | uses all four cells; 0 = guessing, 1 = perfect |
| Cohen's kappa | (observed − chance agreement) / (1 − chance agreement) | | 0.765 | same idea as MCC |

**Precision 68% → 81%: what changed, honestly.** The first model's false alarms were mostly in fast error bursts with a 1 s slot wait: lots of failures and retries, but users still being served and no work piling up. Two fixes: (1) features that see useful work and the build-up directly (`useful_ratio`, `d_concurrency_ratio`) and logs of the heavy-tailed ratios; (2) a threshold chosen by nested cross-validation instead of a fixed 0.5. False alarms fell from 86 to 41 and accuracy rose from 91.8% to 94.1%. The price: recall fell from 84.7% to 79.2% (45 missed windows instead of 33), but **every one of the 30 episodes is still warned about, a median 8 s ahead**, and healthy-traffic alarms fell from 8 to 0.

`evaluate_model.py` computes these formulas by hand and checks them against scikit-learn (largest difference 1.1 × 10⁻¹⁶). `show_metrics.py` prints the confusion matrix and every formula with the numbers plugged in, in plain Python (even ROC-AUC and PR-AUC, and the curves it draws). That's the one to run in the demo.

### 8.3 Ranking quality, with no threshold at all

- **ROC-AUC = 0.954.** Pick one random window where livelock came and one where it didn't; 95% of the time, the model gave the first one the higher risk. 0.5 = coin flip.
- **PR-AUC = 0.853.** More honest when positives are rare. Chance level is the positive rate, **0.148**.

### 8.4 Calibration: does "risk 0.8" mean 80%?

| risk | windows | livelock really came |
|---|---|---|
| 0.0-0.1 | 816 | 1% |
| 0.3-0.4 | 93 | 10% |
| 0.5-0.6 | 17 | 18% |
| 0.6-0.7 | 23 | 39% |
| 0.8-0.9 | 30 | 70% |
| 0.9-1.0 | 167 | 86% |

The risk is **higher than reality** in the middle: class balancing makes positives count more, which pushes risks up. It does **not** change the order (ROC-AUC 0.95), and the threshold (0.65) was chosen for these risks, not assumed. Brier score **0.064** (skill 0.49), ECE **0.093**.

### 8.5 How sure are we? Bootstrap confidence intervals

1. From the 28 runs, draw 28 **with replacement**.
2. Recompute the metric on that resample.
3. Repeat **1,000 times**; the 2.5th and 97.5th percentiles give the **95% interval**.

We resample **whole runs**, not single windows, because windows of one run are not independent.

| Quantity | Value | 95% interval |
|---|---|---|
| Accuracy | 0.941 | 0.922 to 0.960 |
| Precision | 0.807 | 0.687 to 0.884 |
| Recall | 0.792 | 0.712 to 0.871 |
| F1 | 0.799 | 0.721 to 0.860 |
| MCC | 0.765 | 0.681 to 0.834 |
| ROC-AUC | 0.954 | 0.904 to 0.979 |
| F1 gain over the reactive rule, before onset | +0.40 | **+0.34 to +0.45** |
| PR-AUC gain from graph features, before onset | +0.08 | **+0.02 to +0.16** |
| PR-AUC gap to the random forest | −0.03 | −0.05 to −0.01 |

If the whole interval of a difference is above 0, the gain survives resampling. Both main claims (beats the reactive rule, the graph helps) pass. The forest ranks slightly better (PR-AUC 0.88) but is no better at its threshold (F1 0.79).

### 8.6 Moving the threshold (on the out-of-fold risks)

| threshold | precision | recall | F1 | healthy alarms | warned |
|---|---|---|---|---|---|
| 0.4 | 0.61 | 0.88 | 0.72 | 5.7% | 30/30 |
| 0.5 | 0.72 | 0.85 | 0.78 | 0.2% | 30/30 |
| **0.65 (final model)** | 0.80 | 0.82 | 0.81 | 0.0% | 30/30 |
| 0.8 | 0.84 | 0.76 | 0.80 | 0.0% | 30/30 |
| 0.9 | 0.86 | 0.67 | 0.75 | 0.0% | 25/30 |

This table is only for understanding the trade-off; the reported numbers use the nested thresholds, never this table.

### 8.7 Where it works and where it doesn't

| Group | Positives | Precision | Recall | F1 | MCC |
|---|---|---|---|---|---|
| latency faults | 145 | 0.90 | 0.88 | 0.89 | 0.87 |
| slot wait 4-10 s | 129 | 0.95 | 0.86 | 0.90 | 0.88 |
| gateway→order | 185 | 0.83 | 0.86 | 0.85 | 0.75 |
| slot wait 1 s | 87 | 0.63 | 0.69 | 0.66 | 0.61 |
| error faults | 71 | 0.61 | 0.61 | 0.61 | 0.55 |
| order→inventory | 26 | 0.65 | 0.42 | 0.51 | 0.50 |
| **order→payment** | **5** | 0.25 | 0.20 | 0.22 | 0.22 |
| no-fault controls | 0 | 0 false alarms in 180 windows | | | |

Best on the self-sustaining case it was built for (latency faults, long queues). Weaker on error faults, where storms are short. order→payment has only **5** positive windows in the whole dataset, too few to learn or to judge.

### 8.8 What it costs at run time

| | Logistic regression (ours) | Random forest |
|---|---|---|
| one prediction | **3.1 µs** by hand (about 150 µs via scikit-learn) | **5,846 µs** |
| one whole controller round (3 edges: features, risk, decision) | **27 µs** = 0.003% of the 1 s tick | |
| training on 1,458 windows | 9 ms | 233 ms |
| size | 56 numbers, 2.4 KB | 100 trees, 6,012 nodes, about 0.5 MB |

(Timings vary a little from machine to machine and run to run.)

### 8.9 The shuffled-label control, repeated 20 times

Train the same model on randomly shuffled labels: it can't learn anything real. Done 20 times: ROC-AUC 0.36 to 0.62 (mean 0.48), PR-AUC 0.12 to 0.33. None came near ours (0.95 / 0.85).

### Check yourself (Chapter 8)

1. Compute precision and recall from the confusion matrix.
2. Why is 94% accuracy not enough on its own here?
3. What does ROC-AUC 0.95 mean in one sentence?
4. What exactly raised precision from 68% to 81%, and what did it cost?
5. Why resample runs rather than windows in the bootstrap?
6. Why can't we just pick the best threshold from the table in 8.6?

---
## 9. The controller: how it decides (`controller.py`)

### 9.1 The loop, once per second

```
every 1 s:
  1. read only the NEW lines of the call logs          (tail reader)
  2. keep the last 4 s of call records
  3. per edge: stats for the last 2 s, and for the 2 s before that
  4. predictive mode: features (same features.py) → risk from model.json
  5. decide per edge: CUT, HOLD or RESTORE              (control_step)
  6. if anything changed: write retry_policy.json       (atomic replace)
  7. print one line and log the decision to controller_log.jsonl
```

**Step 1, the tail reader** (`common/telemetry.py`, `read_new_call_records()`). Re-reading 100 MB of logs every second would get slower and slower. Instead the controller remembers the byte offset where it stopped in each file and reads only what was appended since. Two careful details: it opens files in **binary** mode, so byte offsets are exact on Windows (text mode turns `\r\n` into `\n` and the offsets drift), and it holds back a **half-written last line** until its newline arrives. It starts at the end of each file, so old runs are ignored.

**Step 3** uses the same `features.py` functions as training (`live_window_stats()` is just `window_stats_for_edge()` for one window), so the numbers mean exactly what they meant to the model.

### 9.2 Deciding "risky"

From `controller.py`:

```python
def decide_risky(mode, edge, stats, prob, values, model):
    if stats["n_calls"] < MIN_CALLS:          # fewer than 3 calls: nothing to judge
        return False
    reactive = reactive_alarm(stats)          # rho > 1 and overload > 20%
    if mode == "reactive":
        return reactive
    predicted = prob is not None and prob >= model["threshold"] and congestion_starting(edge, values)
    return reactive or predicted
```

- **Reactive mode:** risky if a storm is happening **right now**: ρ > 1 (more retries than fresh calls) **and** overload > 20% (many 503s or timeouts). Both ingredients are needed: retries into a callee with spare capacity are fine.
- **Predictive mode:** risky if the model's risk ≥ its threshold (0.65, from `model.json`) **and** congestion is starting, **or** the reactive rule fires (kept as a safety net).

**"Congestion starting"** (`congestion_starting()`) means the callee side is filling up:
- overload ≥ 10% on this edge, or on an edge below it; or
- for a leaf edge: its latency has at least doubled versus healthy;
- for gateway→order: the latency of the edges **below** it has at least doubled. Not its own latency, because order's own response time includes its retry back-off sleeps, which isn't congestion.

### 9.3 Cutting and restoring

```
CUT      attempts − 1 (never below 1), backoff × 2 (at most × 4)
         at most once every 2 s per edge          (CUT_COOLDOWN_SEC = 2)

RESTORE  attempts + 1, backoff ÷ 2, one step at a time
         only if: not risky now,
                  "healthy" for 3 checks in a row  (RESTORE_TICKS = 3; healthy = ≥ 3 calls and overload < 10%)
                  and ≥ 4 s since the last cut     (HOLD_SEC = 4)
```

So 3 attempts → 2 → 1, and backoff 100 → 200 → 400 ms. Then back up one step at a time once things are calm.

**Path rule.** If an edge is risky, the edge **above** it is marked for cutting too. Retries multiply down a chain (3 × 3 = 9 calls to a leaf), so cutting the edge above removes that multiplier. This is why the deck calls the controller "path-aware".

**This is AIMD**, the shape of TCP congestion control: react **fast** to congestion (cut), give back **slowly** when it clears (one step after 3 calm seconds). Flip it around (cut slowly, restore fast) and you get oscillation.

### 9.4 Why restore on "overload cleared", not on "ρ is low"

Our Review-2 controller restored retries when ρ dropped. But once attempts are cut to 1, **ρ is 0 by definition** (no retries are allowed). So it saw ρ = 0, gave retries back while the callee was still drowning, the storm came back, it cut again… on/off flapping. The fix: restore only when **overload** (503s and timeouts) has cleared. Overload is measured on fresh calls too, so it stays honest even with retries off. There's a unit test for exactly this (`test_no_flapping_when_retries_are_off`).

### 9.5 Why errors alone don't count as congestion

If a callee returns 500 errors **fast** and has free slots, a retry is a fresh chance to succeed and uses capacity nobody else needs. Retries only hurt when the callee is **out of capacity**. So the congestion signal is overload, not failure. That's also why "errors with spare capacity" must not block a restore (`test_errors_with_spare_capacity_do_not_cut`).

### 9.6 The congestion gate: a lesson from our own experiments

Our first predictive mode (v1) cut whenever risk ≥ 0.5, with no gate. In the metastable experiment it worked well. In the error burst it served only **52.7%** of users during the fault, worse than the reactive mode (68.1%) and no better than the fixed budget.

We replayed the logged decisions (`data/controller_log.jsonl`) to find out why. The model's strongest input is the fresh-call failure ratio (weight +1.89): in the training data, errors usually came before storms, so it read "payment is returning errors" as "livelock coming". But in an error burst, payment answers **fast** with free slots, so a retry does no harm. **144 of the 152** model-only cuts happened with no congestion at all.

The fix applies the controller's own principle to predictive mode: **cut on congestion, not on errors.** A predicted cut now also needs the callee side to be filling up (9.2). In the replay this removed 144 of the 152 unnecessary cuts and kept every early cut in the metastable runs.

Because the gate was designed by looking at those logs, judging it on the same runs would be cheating. So **all experiments were re-run from scratch**, and every final number comes from the new runs. The v1 results are kept in `results/history/`.

### 9.7 What it looks like live

From our rehearsal of live run 2 with the current model (`t` = seconds since the controller started; the scenario started about 2 s later, so the ramp started at about t = 12 s):

```
t=+  14s | gw->order rho 0.00 risk 0.62 -> 3 | order->pay rho 0.03 risk 0.04 -> 3 | ...
t=+  15s | gw->order rho 0.00 risk 0.91 -> 2 CUT | ...
t=+  18s | gw->order rho 0.24 risk 0.88 -> 1 CUT | ...
t=+  24s | ... | order->pay rho 1.75 risk 0.00 -> 2 CUT | ...
t=+  29s | ... | order->pay rho 1.20 risk 0.00 -> 1 CUT | ...
t=+  34s | ... | order->pay rho 0.00 risk 0.00 -> 2 RESTORE | ...
t=+  37s | ... | order->pay rho 0.03 risk 0.00 -> 3 RESTORE | ...
```

- **15 s:** risk 0.91 ≥ 0.65 and payment's latency has doubled, so gateway→order is cut 3 → 2, **while ρ is still 0.00**. No reactive rule could fire here. This is the pre-emptive cut.
- **18 s:** the 2 s cooldown has passed and it's still risky, so 2 → 1.
- **19-22 s:** ρ on gateway→order briefly rises (up to 1.48): those are retries of calls that started before the cut. It falls to 0 within 3 s.
- **24 s and 29 s:** order→payment gets its own storm (ρ 1.75 with overload), so the **reactive safety net** cuts it twice. (Its own risk is low: the model has few examples of storms on that edge, Chapter 8.7.)
- **34 s, 37 s:** order→payment has been calm for 3 checks and more than 4 s since the last cut, so one step is given back each time.
- gateway→order stays at 1 attempt until overload has fully cleared, then comes back step by step (about 53 s and 56 s).

In the 5 metastable experiment runs the predictive controller always made its first cut at **11.5 s** (1.5 s into the ramp). The reactive controller first cut at **17.5-19.5 s**, after the storm had formed.

### 9.8 Safety

- When the controller stops (time up, or Ctrl+C), it writes the **original** policy back, so the services are never left with retries switched off (`--keep_policy` disables this).
- If it's killed outright, the services keep their last good policy, and the next `run_all.py` deletes the policy file anyway.
- A cut that wasn't needed costs a few requests that a retry would have saved, which is why restores are automatic and the gate exists.

### Check yourself (Chapter 9)

1. Why does the controller read logs with byte offsets, in binary mode?
2. State the predictive "risky" rule in one sentence.
3. Why does gateway→order look at the latency of the edges below it?
4. Why was restoring on low ρ a bug?
5. What is the path rule and why does it exist?
6. Tell the congestion-gate story in four sentences, including why everything was re-run.

---

## 10. The experiments: what we compared and what we found (`experiment_arms.py`)

### 10.1 Design

The **same fault**, hit with **five strategies**, everything else identical:

| Strategy | Implementation |
|---|---|
| `no_retry` | policy file with 1 attempt on every edge |
| `static_retry` | 3 attempts everywhere (the common default) |
| `retry_budget` | 3 attempts, but retries capped at 10% of fresh calls per edge (token bucket, 1.9) |
| `reactive` (ours) | 3 attempts + `controller.py --mode reactive` started as a separate process |
| `predictive` (ours) | 3 attempts + `controller.py --mode predictive` |

**Two faults**, chosen because they pull in opposite directions:

| | metastable | error_burst |
|---|---|---|
| fault | payment latency ramps to 700-900 ms over 10 s (5 steps), held 10 s | payment error rate ramps to 70% over 10 s, held 10 s |
| order's slot wait | 10 s (long queue) | 1 s (short queue) |
| load | 20 users/s, Poisson | 20 users/s, Poisson |
| watched after the fault | 25 s | 15 s |
| the question | does the system recover after the fault is gone? | retries are **useful** here. Does a strategy keep that benefit? |

**5 strategies × 2 faults × 5 repetitions = 50 runs**, in **shuffled order** (seed 11). Shuffled so that slow drift of the machine over the hour (heat, background tasks) can't favour one strategy. Between runs: wait until idle, reset the policy.

### 10.2 How the numbers are measured

For each run, three periods: **healthy** (the normal phase), **fault** (ramp + held fault, 20 s), **after** (the recovery phase).

- **Success %**: users who **arrived** in the period and got a 200.
- **Calls**: every internal call attempt that started in the period (how hard the system worked).
- **Calls per success**: work per useful result.
- **Orphaned %**: of order_service's requests that got a slot in the period, the fraction that finished after the gateway's deadline.
- **p95 latency**: 95% of users waited less than this.
- **Recovery time**: seconds after the fault was removed until users get at least **90% of that run's own healthy success rate for 5 seconds in a row**. Never recovered = the full 25 s.

**Statistics.** Mean ± standard deviation over the 5 runs, and a **Welch t-test** for the key comparisons. A t-test asks: if the two strategies were really the same, how likely is a difference in means at least this big, just by chance? That's the **p-value**; below 0.05 is the usual line for "significant". "Welch" means it doesn't assume the two groups have equal spread. With only 5 runs per group, small real differences can easily come out "not significant", so we only claim the clear ones.

### 10.3 Metastable results

| | healthy | during fault | after fault | recovered | recovery time | calls in fault | orphaned after | livelock formed |
|---|---|---|---|---|---|---|---|---|
| no retry | **89.9%** | 5.3% | 45.5 ± 6.4% | 5/5 | 12.0 s | 662 | 63.3% | 0/5 |
| static retry | 100% | 6.0% | **2.9 ± 0.3%** | **0/5** | never | 1,212 | **98.2%** | **5/5** (79 windows) |
| retry budget 10% | 100% | 5.3% | 23.7 ± 12.9% | 3/5 | 20.4 s | 682 | 85.2% | 0/5 |
| reactive (ours) | 100% | 5.2% | 40.6 ± 13.6% | 5/5 | 14.8 s | 973 | 71.0% | 5/5 (12 windows) |
| predictive (ours) | 100% | 5.7% | **48.3 ± 10.4%** | **5/5** | **13.0 s** | 676 | 63.7% | 1/5 (1 window, order→payment) |

How to read it, strategy by strategy:
- **During the fault**, everyone is at 5-6%. That's payment's capacity (2.5/s); no policy can change it.
- **Static retry**: the textbook metastable failure. The fault is gone and the system stays stuck for the whole 25 s, nearly all work orphaned.
- **No retry** recovers fast (the inequality 20 < 32 holds at once), but loses about 10% of users all the time, because healthy services fail 5% of calls (0.98 × 0.95 × 0.95 ≈ 0.884).
- **Retry budget**: safe-ish (retries are capped), but recovery varied a lot run to run (±12.9) and 2 of 5 runs didn't recover within 25 s.
- **Reactive**: fixes the storm, but only after it has formed (livelock in all 5 runs), so a backlog builds first and has to drain, with 44% more calls during the fault than predictive.
- **Predictive**: 100% when healthy, recovered in all 5 runs and fastest (13.0 s), the most users served after the fault (48.3%), and the self-sustaining livelock on gateway→order **never formed**. One run had a single 2 s livelock window on order→payment during the fault, which the reactive safety net cut.

**Significance** (predictive vs …): vs static, success after p = 0.001, recovery time p < 0.001, calls p < 0.001 (very clear). vs budget: success after p = 0.011, recovery time p = 0.018 (significant), calls p = 0.68 (the same). vs reactive: success p = 0.34, recovery p = 0.29 (not significant), but calls p < 0.001 (676 vs 973) and livelock on gateway→order 0/5 vs 5/5.

### 10.4 Error-burst results

| | healthy | during fault | calls in fault | calls per success | p95 latency in fault |
|---|---|---|---|---|---|
| no retry | 89.0% | **39.0%** | 1,194 | 7.6 | 287 ms |
| static retry | 100% | **74.0%** | 1,690 | 5.7 | 4,036 ms |
| retry budget 10% | 100% | 53.1% | 1,359 | 6.6 | 618 ms |
| reactive (ours) | 100% | 69.2% | 1,589 | 5.7 | 3,950 ms |
| predictive (ours) | 100% | 71.1% | 1,552 | **5.4** | 3,659 ms |

Why retries **help** here: payment fails fast and still has free slots, so each attempt is a fresh, independent chance. At the 70% peak, one payment attempt succeeds 30% of the time; with 3 attempts, 1 − 0.7³ ≈ **66%**. So static retry is the best strategy, and no retry the worst.

- The **budget** throws that benefit away: 53.1% (p < 0.001 vs predictive). It can't tell a useful retry from a harmful one.
- **Predictive** keeps 71.1% against static's 74.0% (p = 0.33, not a significant difference), with the **fewest calls per successful user**. That's the congestion gate at work: no congestion, so no cuts.
- Every strategy recovers the moment the fault ends: with a 1 s queue, there are no orphans to clear.
- **Honest cost:** keeping retries on means the slowest users wait longer (p95 about 3.7 s vs 0.6 s for the budget).

### 10.5 The finding

| | healthy | metastable | error burst | verdict |
|---|---|---|---|---|
| no retry | loses ~10% | recovers | worst (39.0%) | bad even when nothing is wrong |
| static retry | 100% | never recovers | best (74.0%) | catastrophic in the metastable case |
| retry budget | 100% | 3/5 recover | 53.1% | throws away useful retries |
| reactive | 100% | 5/5, after livelock forms | 69.2% | fixes the storm after it forms |
| predictive | 100% | 5/5, fastest, livelock doesn't form | 71.1% | the only one never bad |

**Retries help with short, fast failures and hurt during saturation. A fixed retry policy can't be right in both situations, so the retry budget has to adapt while the system runs, and acting before the storm forms beats reacting to it.**

### Check yourself (Chapter 10)

1. Why two faults, and why these two?
2. Why are the 50 runs shuffled?
3. Define recovery time precisely.
4. Why does no-retry recover fastest, and why is it still not the answer?
5. Why is static retry best in the error burst? Show the 1 − 0.7³ calculation.
6. Which differences are statistically clear, and which aren't?

---

## 11. How the project evolved: what went wrong and what we fixed

Faculty like to see the process, not only the result. These are the real changes, each with the reason.

| What was wrong earlier (Review-2 version) | Why it mattered | What we changed |
|---|---|---|
| The controller restored retries when ρ dropped | at 1 attempt ρ = 0 by definition, so it flapped on and off | restore only after overload has cleared, plus a hold time (unit-tested) |
| The load generator waited for each phase's requests to finish before starting the next | gave the system a free break exactly when the fault was removed, which **hid the self-sustaining case** | continuous open-loop Poisson traffic |
| We claimed a self-sustaining livelock was impossible | it was only hidden by the pause | it needs orphaned work (a long queue): now shown live and explained by the capacity inequality |
| C4 used a strict trend test; C3 used one absolute threshold for every edge | strict trend = coin flip in a steady storm (only 45 of 128 windows found); absolute thresholds don't fit edges with different traffic | 20% tolerance (justified by the sweep); C3 relative to each edge's own healthy level |
| The model was tested on a time split with only 18 positives, no baseline; the graph was drawn but not used | the result couldn't be trusted, and the "graph" contributed nothing | grouped CV, 216 positives, baselines, ablations, before-onset scores, lead time; graph features that are measured |
| The controller didn't use the prediction; the only baselines were "no retry" and "always retry" | the prediction had no purpose | predictive mode with the congestion gate; a 10% retry-budget baseline |
| One run per strategy; no jitter in the backoff | one run proves nothing; no jitter isn't realistic | 5 runs each, shuffled, mean ± std, Welch t-tests; equal jitter |
| The model was judged only on precision, recall and F1 | not enough to trust it | full evaluation: MCC, ROC-AUC, calibration, bootstrap intervals, threshold sweep, per-group results, run-time cost |
| Ctrl+C left the fault on; leftover work leaked into the next run; no tests or data checks | broken runs, mixed-up data | safe stop, wait-until-idle between runs, scenario ids used once, 50 unit tests, `validate_data.py` |

And within the final round, **the congestion gate** (Chapter 9.6): v1 predictive cut on errors in the error burst; we found it by replaying logs, fixed it, and re-ran everything so the fix is judged on fresh data.

The pattern to point out if asked: **every fix came from measuring something and finding it didn't hold up**, and every result we report was produced after the fix, not tuned on the data it's reported on.

---

## 12. Limitations: what's general and what's specific to our testbed

Say these before she asks; it shows you know where the edges are.

| Limitation | What we did about it / why it's acceptable |
|---|---|
| One machine, Python threads, Flask development server | absolute numbers (capacities, latencies) belong to this testbed; we explain the **mechanism** (the capacity inequality), which is general |
| A 2-level call chain | amplification multiplies per level (3 × 3 = 9), so real deeper chains are worse; our numbers are a lower bound |
| Labels come from our own definition | no external ground truth exists; validated against user success (9% vs 68%), the fault timeline, and a sensitivity sweep |
| Logistic regression, not a temporal graph network | the random forest shows headroom (0.80 vs 0.75); a stronger model is the next step |
| The model is trained on data recorded under static retries, but the controller changes the policy at run time, so what the model sees shifts | the reactive safety net stays on, and restoring depends on overload, not on the model |
| 5 runs per strategy | p-values reported; reactive vs predictive success isn't claimed as significant |
| The gate's thresholds were chosen by replaying the first experiment | all final numbers come from completely new runs |
| A fault inside order_service itself would only be caught by the reactive safety net | the gate for gateway→order looks at the edges below; stated openly |
| The risk values aren't calibrated probabilities (ECE 0.10); order→payment has only 5 positive windows | ranking is unaffected (ROC-AUC 0.95); both reported, not hidden |

**What's general:** retries multiply down a chain; a timeout without cancellation creates orphaned work; a long queue lets orphans outlive the fault; the exit condition *arrival rate × amplification < capacity*; and a retry is useful against fast failures and harmful under saturation. Those are true of any system with retries and bounded worker pools.

---

## 13. Code map: where everything is

### 13.1 How the files depend on each other

```
common/config.py ─────────────── used by everything (ports, edges, defaults, paths)
common/safe_files.py ─────────── used by fault_injection, retry_policy, telemetry (atomic JSON)
common/telemetry.py ──────────── used by services, scenario_runner, all analysis, controller
common/fault_injection.py ────── used by services (do_work) and scenario_runner (write_config)
common/retry_policy.py ───────── used by retry_client (read), controller and experiments (write)
common/retry_client.py ───────── used by gateway and order_service
common/service_helpers.py ────── used by the 4 services

services/*.py ←── started by run_all.py
scenario_runner.py ←── used by generate_dataset.py and experiment_arms.py
features.py ←── used by labeling, prediction_model, evaluate_model, graph_builder, controller
labeling.py → data/labeled_windows*.csv, data/baselines.json
prediction_model.py → results/model.json, prediction_oof.csv
evaluate_model.py ← prediction_oof.csv, model.json
controller.py ← model.json → data/retry_policy.json
experiment_arms.py → runs controller.py as a subprocess
run_pipeline.py → runs the 9 steps in order
```

### 13.2 A good reading order (for learning the code)

1. `common/config.py` (all the settings)
2. `services/payment_service.py`, then `services/order_service.py`, then `services/gateway.py`
3. `common/retry_client.py` (the retry loop) and `common/fault_injection.py`
4. `common/telemetry.py` (logs) and `scenario_runner.py` (one run)
5. `features.py` (the maths, read slowly)
6. `labeling.py` (`check_conditions`, `label_scenario`)
7. `prediction_model.py` (`build_examples`, `cross_validate`, `lead_times`)
8. `controller.py` (`control_step`, `decide_risky`, `congestion_starting`)
9. `experiment_arms.py` (`run_one`, `run_metrics`, `recovery_time`)
10. `evaluate_model.py` and `validate_data.py` last

`docs/CODE_WALKTHROUGH.md` walks through the same order in more detail.

### 13.3 "Where is X?" (she may ask you to open the file)

| Question | File, function |
|---|---|
| Where are the slots? | `services/*.py`: `worker_slots = threading.Semaphore(n)`, used as `worker_slots.acquire(timeout=queue_wait)` |
| Where is a retry made? | `common/retry_client.py`, `call_with_retry()` |
| Where is the backoff with jitter? | `common/retry_client.py`, `backoff_with_jitter_ms()` |
| Where is the retry budget? | `common/retry_client.py`, `budget_deposit()` / `budget_take()` |
| How do services read the policy? | `common/retry_policy.py`, `get_retry_policy()`, on every call |
| Where is the fault applied? | `common/fault_injection.py`, `load_config()` and `do_work()` |
| Where is "orphaned" decided? | `common/telemetry.py`, `log_server_request()`: `orphaned = done_time > deadline` |
| Where is ρ computed? | `features.py`, `finish_window()` |
| Where are the four conditions? | `labeling.py`, `check_conditions()` |
| Where is the healthy baseline? | `labeling.py`, `edge_baseline()` |
| Where are the 18 features built? | `features.py`, `make_features()` |
| Where is the graph message passing? | `features.py`, `make_features()` (the `down_` and `up_` features) |
| Where is the label "2-12 s ahead" built? | `prediction_model.py`, `build_examples()` |
| Where is the grouped cross-validation? | `prediction_model.py`, `cross_validate()` (GroupKFold) |
| Where is the risk computed live? | `features.py`, `risk_probability()` |
| Where does the controller decide? | `controller.py`, `control_step()`, `decide_risky()`, `congestion_starting()` |
| Where are cut and restore? | `controller.py`, `cut()`, `restore()` |
| How does the controller read logs fast? | `common/telemetry.py`, `read_new_call_records()` |
| Where is recovery time computed? | `experiment_arms.py`, `recovery_time()` |
| Where is the bootstrap? | `evaluate_model.py`, `bootstrap()` / `resample_scenarios()` |
| Where are the confusion matrix and accuracy worked out step by step? | `show_metrics.py`, `count_cells()`, `metrics_from_cells()`, `roc_auc_by_hand()`, `pr_auc_by_hand()` |
| Where are the safe writes? | `common/safe_files.py`, `write_json_atomic()` |

### 13.4 The tests (`tests/test_core.py`, 50 tests)

They cover, in groups: the window maths (ρ, capping, concurrency spread over windows), the four conditions (healthy is not livelock, a storm meets all four, a recovering storm fails C4, overload without retries is not livelock, too few calls aren't judged, 2 windows in a row needed), the controller (a cut also cuts the parent, cooldown, **no flapping when retries are off**, slow restore, fast errors don't cut, predictive mode ignores fast errors), the policy file (missing, corrupt, nonsense values, atomic write), the retry client (gives up after max attempts, stops on success, budget empties and refills, jitter in range), the log reader (partial lines, resets, damaged lines), the scenario schedule, recovery time, lead time, the evaluation maths against scikit-learn, and the optional Alibaba reader. Run: `python -m unittest discover tests`.

---

## 14. Glossary

| Term | Meaning |
|---|---|
| **AIMD** | additive increase, multiplicative decrease: TCP's rule of backing off fast and probing slowly. Our controller cuts fast and restores one step at a time |
| **amplification (ρ)** | retries ÷ fresh calls on an edge in a window |
| **attempt** | one try of a call; attempt 1 is fresh, 2 and 3 are retries |
| **backoff / jitter** | waiting before a retry, doubling each time / adding randomness so clients don't retry in lock-step |
| **baseline (S\*, N\*)** | an edge's healthy level, from the first 8 s of the same run |
| **bistable** | two stable states under the same conditions; history decides which |
| **bootstrap** | resampling (here: whole scenarios, 1,000 times) to get a confidence interval |
| **call edge** | a caller→callee link: gateway→order, order→payment, order→inventory |
| **class balancing** | weighting the rare class more during training |
| **congestion gate** | predicted cuts also need the callee side filling up (latency doubled or ≥ 10% overload) |
| **counting semaphore** | a counter of permits; our worker slots |
| **episode** | consecutive livelock windows on one edge of one run |
| **fault-coupled livelock** | a storm that lasts only as long as the fault (short queue) |
| **grouped cross-validation** | CV where all windows of one run stay in the same fold |
| **lead time** | seconds of warning before an episode starts |
| **Little's Law** | N = X × R: average in flight = throughput × time inside |
| **livelock** | everything running, nothing progressing |
| **metastable failure** | a bad state that a trigger starts and a sustaining effect keeps going after the trigger is gone |
| **open loop** | load that keeps arriving no matter how slow the system is |
| **orphaned work** | work finished after the caller had already given up |
| **out-of-fold score** | a prediction made by a model that never saw that example's run |
| **overload ratio** | fraction of attempts that got 503 (busy) or timed out |
| **path rule** | when an edge is cut, the edge above it is cut too |
| **retry budget** | cap retries at a fraction of fresh calls (token bucket) |
| **slot wait / queue_wait** | how long a request may wait for a worker slot before a 503 |
| **Utilization Law** | U = X · S / c ≤ 1 |
| **window** | a 2-second slice of time on one edge |

---

## 15. Self-test

Try to answer out loud, as if she asked. Click to check.

<details><summary>1. Explain the project in 30 seconds.</summary>
When a service slows down, callers retry, the retries overload it more, and the system becomes 100% busy but serves nobody: livelock. With long queues it can outlive the fault. We built a 4-service testbed where this happens by itself, a formal detector, a model that predicts it about 8 s ahead, and a controller that cuts retries per edge before the storm forms. Static retries never recovered; with our controller the system recovered every time.
</details>

<details><summary>2. Livelock vs deadlock vs overload?</summary>
Deadlock: blocked, idle, circular wait. Livelock: running, 100% busy, no useful work, caused by failures creating work. Overload: running, busy, but useful work up to capacity, ends when load drops. Our C1 (ρ > 1) and C4 (loop not damping) separate livelock from overload.
</details>

<details><summary>3. Why does the storm outlive the fault?</summary>
With a 10 s queue, abandoned requests stay queued and get processed later for nobody (orphans). After the fault, capacity is about 32/s but 3 attempts × 20 users/s = up to 60/s arrive, and since a ~200 backlog takes longer to drain than the 2 s timeout, nearly every request served is already abandoned, so users keep failing and retrying. 60 > 32: it never drains.
</details>

<details><summary>4. Why does a 1 s queue not give a self-sustaining storm?</summary>
A request that can't get a slot is rejected with 503 after 1 s, before the gateway's 2 s timeout, so nothing stale stays queued to be processed later. No orphan backlog, so it recovers when the fault ends.
</details>

<details><summary>5. What is orphaned work and how do you measure it?</summary>
Work finished after the caller gave up. The caller sends its deadline in the X-Deadline header; the callee logs orphaned = (finish time > deadline).
</details>

<details><summary>6. Why didn't you use a public dataset?</summary>
None has per-attempt retry numbers, outcomes and fault timing together. The OSDI 2022 work that defined this failure class generated its data by running a system and injecting a trigger, which is what we do.
</details>

<details><summary>7. Why can a generated dataset be trusted?</summary>
Only the fault is injected; the storm emerges from a real system. Exact ground truth. 6 groups of validation checks pass (bookkeeping, retry rules, Utilization Law, fault fidelity, load rate, timestamps). Regenerating it gives 131-135 livelock windows each time.
</details>

<details><summary>8. State the four conditions.</summary>
C1 ρ > 1. C2 useful throughput < half of healthy. C3 calls in flight > double healthy and not falling (within 20%). C4 retries not falling (within 20%) and success not improving (by more than 5 points), compared with 4 s ago. All four, 2 windows in a row, at least 4 calls.
</details>

<details><summary>9. Why not just use ρ?</summary>
ρ alone can't tell a working retry burst from a storm, and C4 alone is true on healthy traffic (ds_06 at 8 s). The combination is what's specific to livelock.
</details>

<details><summary>10. How do you know the labels are right?</summary>
9% user success in flagged windows vs 68% in other bad windows; 0 false alarms in 636 healthy windows including high-load controls; smooth sensitivity with 0 false alarms at every threshold.
</details>

<details><summary>11. Why does the storm show up on gateway→order and not order→payment?</summary>
Order's 8 slots cap how many retries can pile up towards payment (ρ ≈ 1 there). The gateway has no limit, so its retries pile up on order (ρ ≈ 2, 100+ in flight). The detector shows where the storm is, not where the fault is.
</details>

<details><summary>12. What exactly does the model predict?</summary>
For each edge, every 2 s: will this edge be in livelock in any of the next 5 windows (2-12 s ahead)? The current window isn't part of the label.
</details>

<details><summary>13. Where is the graph used?</summary>
In the features: one round of message passing. Each edge gets the worst overload, latency ratio and fail ratio of the edges below it, and the ρ and overload of the edge above. Removing them drops before-onset PR-AUC from 0.78 to 0.70 and F1 from 0.72 to 0.64.
</details>

<details><summary>14. How did you avoid data leakage?</summary>
Grouped 5-fold CV by scenario, so no window of a test run is ever in training; the scaler is fitted on training folds only; a shuffled-label control scores at chance.
</details>

<details><summary>15. Why logistic regression and not the random forest?</summary>
The controller runs it every second: about 3 µs per prediction by hand, 56 numbers. The random forest is not better (F1 0.79 vs 0.80), about 1,900× slower and about 0.5 MB. A stronger model is future work.
</details>

<details><summary>16. Your accuracy is 94%. Is that good?</summary>
Not on its own: always saying "no" gets 85%. MCC 0.77 (0.68-0.83), balanced accuracy 0.88 and precision 0.81 / recall 0.79 are the honest numbers.
</details>

<details><summary>17. What does ROC-AUC 0.95 mean?</summary>
95% of the time, a random window where livelock came gets a higher risk than a random window where it didn't.
</details>

<details><summary>18. Is risk 0.8 an 80% chance?</summary>
No. Class balancing pushes risks up; 0.8-0.9 was followed by livelock 70% of the time (ECE 0.09). The ranking is still good, and the threshold (0.65) was chosen by nested cross-validation for these risks.
</details>

<details><summary>19. How early does it warn?</summary>
Before 30 of 30 episodes, median 8 s (range 2-10 s); the reactive rule warned before 24, median 2 s. Alarms in healthy windows: 0 of 576.
</details>

<details><summary>20. Explain the controller in 30 seconds.</summary>
Every second, per edge: stats for the last 2 s and the model's risk. Risky = (risk ≥ 0.65 and congestion starting) or (ρ > 1 and overload > 20%). Risky: cut one attempt and double the backoff, and also cut the edge above (path rule), at most every 2 s. Calm for 3 checks and 4 s since the last cut: give back one step. Cut fast, restore slowly, like TCP's AIMD.
</details>

<details><summary>21. Why not restore when ρ is low?</summary>
At 1 attempt ρ is 0 by definition, so that rule restores while the callee is still overloaded and the controller flaps. We restore when overload clears.
</details>

<details><summary>22. What is the congestion gate and why does it exist?</summary>
Predicted cuts also need the callee side filling up (latency doubled or ≥ 10% overload). Without it, v1 cut useful retries during an error burst (52.7% success) because the model treats errors as a warning sign. 144 of 152 cuts had no congestion. All experiments were re-run after the fix.
</details>

<details><summary>23. Why does no-retry lose 12% of users when healthy?</summary>
Healthy services fail on their own (order 2%, payment and inventory 5%): 0.98 × 0.95 × 0.95 ≈ 0.884.
</details>

<details><summary>24. Why is static retry the best in the error burst?</summary>
Payment fails fast with free slots, so every attempt is an independent chance: 1 − 0.7³ ≈ 66% vs 30% with one try. Retries are useful when there's spare capacity.
</details>

<details><summary>25. Why does the retry budget do badly in the error burst?</summary>
It caps retries at 10% of fresh calls no matter what, so it throws away useful retries: 53.1% vs 71.1% for predictive.
</details>

<details><summary>26. Is reactive vs predictive significant?</summary>
On success after the fault, no (p = 0.34 with 5 runs). On calls during the fault, yes (676 vs 973, p < 0.001), and the livelock on gateway→order formed in 0/5 predictive runs vs 5/5 reactive.
</details>

<details><summary>27. How is recovery time defined?</summary>
Seconds after the fault is removed until users get ≥ 90% of that run's own healthy success rate for 5 s in a row; never = 25 s.
</details>

<details><summary>28. Where is the OS in this?</summary>
Livelock, receive livelock, counting semaphores, bounded waiting, processes and threads, IPC over sockets and files, atomic rename, the Utilization Law and Little's Law, and AIMD feedback control.
</details>

<details><summary>29. What's your biggest limitation?</summary>
One machine and a 2-level chain, so absolute numbers belong to the testbed; labels come from our own definition (validated three ways); 5 runs per strategy. The mechanism (the capacity inequality) is general.
</details>

<details><summary>30. What would you do next?</summary>
A temporal graph network; deeper chains on several machines; dropping orphans at the queue using the deadline; learning the cut size; retraining on data recorded with the controller on; real data if a dataset with retry attempts becomes public.
</details>
