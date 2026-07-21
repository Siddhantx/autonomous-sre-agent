# Memory leak / OOM risk

## Symptoms
- `process_resident_memory_bytes` for one service grows monotonically past
  400 MiB and does not fall after GC.
- Eventually: container OOM-kill, restart loop, dropped in-flight requests.
- In the lab: the chaos-injector `leak` scenario allocates and retains.

## Diagnosis
1. `prometheus_range` on `process_resident_memory_bytes` over the last hour —
   a leak is a staircase, load-driven growth is a sawtooth.
2. Identify the leaking process; if it is the chaos sandbox, it is injected.
3. For a real service, `code_search` for caches without eviction, global
   lists, or unbounded queues.

## Remediation
- Injected leak: `reset_chaos_sandbox` releases the retained allocations.
- Real service leak: no safe automatic action — a restart only defers OOM;
  needs a code fix.

## Escalate when
- Memory grows again immediately after a sandbox reset.
- More than one service leaks simultaneously (points at a shared library).
