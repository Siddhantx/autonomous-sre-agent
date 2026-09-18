# APOE Benchmark: Agent vs Human SRE

Time-to-diagnosis comparison on five novel faults (none covered by deterministic rules).

> **Read this first.** The agent column is produced in `--fake-llm` mode: a `ScriptedLLM` replaying a pre-written transcript per scenario. Its sub-second time-to-diagnosis is transcript-replay speed, **not** reasoning speed, and its accuracy is the architecture ceiling, not a model score. Treat this as a pipeline smoke test — it is not a like-for-like comparison against a human SRE. For real model numbers see [`RESULTS-local-llm.md`](RESULTS-local-llm.md).

| Scenario | Agent TTD (s) | Agent correct | Human TTD (s) | Human correct | Speedup |
|---|---|---|---|---|---|
| pool-exhaustion | 0.447 | yes | _fill in_ | _fill in_ | — |
| bad-config | 0.334 | yes | _fill in_ | _fill in_ | — |
| slow-query | 0.330 | yes | _fill in_ | _fill in_ | — |
| poison-pill | 0.331 | yes | _fill in_ | _fill in_ | — |
| disk-fill | 0.332 | yes | _fill in_ | _fill in_ | — |

**Agent mean TTD:** 0.355s · accuracy: 100%

Human timings not yet recorded. Run `python evals/benchmark.py --record-human` to fill them in, or edit this file directly.
