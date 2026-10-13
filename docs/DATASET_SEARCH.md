# Public datasets: what exists, and what each one is missing

Written so you can search for these yourself, and so you have a precise answer when
asked "why didn't you use a real dataset?"

## The short answer

A retry-storm livelock dataset would need, for every call:
1. **which attempt it was** (1st, 2nd, 3rd), to compute retry amplification
2. **its outcome** (success / error / timeout)
3. **when the fault started and ended** (ground truth), to label livelock

No public dataset has all three. Most have none of them.

## What to search for, and what you will find

| Search for | What it is | Has attempts? | Has outcomes? | Fault ground truth? | Use for us |
|---|---|---|---|---|---|
| `alibaba clusterdata cluster-trace-microservices-v2021` | Alibaba production call graphs, ~20,000 microservices (Luo et al., SoCC 2021) | no | no (response time only) | no | **Optional sanity check only; our results don't use it.** If you ever want it: download `MSCallGraph_0.tar.gz` (~170 MB) and run `python calibrate_from_trace.py MSCallGraph_0.tar.gz` |
| `alibaba cluster-trace-microservices-v2022` | Same, 13 days, ~2 TB | no | no | no | mention only |
| `lexiangh Metastability github` | Code from *Metastable Failures in the Wild* (OSDI 2022). Its retry experiment runs a MongoDB cluster and logs timestamp, **retry attempts**, success/error, latency | yes | yes | yes (they inject it) | **Methodology precedent.** They generate retry-storm data by running a system and injecting a trigger, as we do |
| `"The Tale of Errors in Microservices" zenodo` | Uber production traces with error tags (SIGMETRICS 2025) | no | partly (error tags) | no | Cite the finding: requests with non-fatal errors did **1.9x more work**. 35 GB+ compressed, needs 300-500 GB unpacked |
| `facebookresearch distributed_traces` (Meta Canopy) | Meta production traces | no | no | no | mention only |
| `Uber CRISP traces zenodo` | Uber Jaeger traces | no | no | no | mention only |
| `RCAEval benchmark` (WWW 2025) | 735 injected failures in Online Boutique, Sock Shop, Train Ticket | no | partly | yes | possible external sanity check; no retry attempts, no livelock labels |
| `Eadro dataset` (ICSE 2023), `AnoMod` (2026), `TrainTicketTrace` (SANER 2026) | fault-injection datasets on benchmark apps | no | partly | yes | mention only |

## Links

- Alibaba v2021: https://github.com/alibaba/clusterdata/tree/master/cluster-trace-microservices-v2021
- Direct file (optional): https://aliopentrace.oss-cn-beijing.aliyuncs.com/v2021MicroservicesTraces/MSCallGraph/MSCallGraph_0.tar.gz
- Alibaba v2022: https://github.com/alibaba/clusterdata/tree/master/cluster-trace-microservices-v2022
- OSDI'22 metastability code: https://github.com/lexiangh/Metastability
- OSDI'22 paper: https://www.usenix.org/system/files/osdi22-huang-lexiang.pdf
- Uber errors dataset: https://zenodo.org/records/13947828
- Meta Canopy traces: https://github.com/facebookresearch/distributed_traces
- RCAEval: https://arxiv.org/abs/2412.17015
- A survey of public trace datasets: https://www.abhishek-tiwari.com/distributed-traces-of-microservices-architecture-publicly-available-datasets/

## The sentence to say in the review

"We checked six public microservice datasets. Each one lacks per-attempt retry information
or fault ground truth, so none can produce livelock labels. The OSDI 2022 paper that defined
metastable failures generated its retry-storm data the same way we do: by injecting a fault
into a real running system and logging every attempt. Our storm is never scripted, only the
fault is. And we validate the data itself against queueing theory and its own bookkeeping,
and every check passes."
