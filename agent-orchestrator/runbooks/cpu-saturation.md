# CPU saturation across the service fleet

## Symptoms
- `rate(process_cpu_seconds_total[1m])` >= 0.85 cores sustained.
- Rising p99 latency on all endpoints at once (not one service).
- In the lab: usually the chaos-injector `high-cpu` scenario (a busy loop).

## Diagnosis
1. `prometheus_query` for `max(rate(process_cpu_seconds_total[1m]))` and the
   per-service breakdown to find which process burns the CPU.
2. If the burner is the chaos sandbox, it is injected load.
3. If it is a real service, check `code_search` for recent hot paths
   (serialization loops, unbounded retries) and correlate with kafka lag.

## Remediation
- Injected load: `reset_chaos_sandbox` (idempotent clear).
- Real service: no safe automatic action — needs scaling or a code fix.

## Escalate when
- CPU is saturated but no single process accounts for it (noisy neighbor /
  host-level problem).
- Load returns immediately after a sandbox reset.
