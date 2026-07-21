# Redis degraded / cache latency

## Symptoms
- `cache-agent` ping latency >= 250 ms, or connection errors.
- Order-service falls back to Postgres reads — DB load rises in tandem.

## Diagnosis
1. `redis_info` — check `used_memory` vs `maxmemory`, `evicted_keys`,
   `blocked_clients`, and `instantaneous_ops_per_sec`.
2. `redis_slowlog` — a slow `KEYS`/`SMEMBERS` on a huge key is the classic
   culprit.
3. `redis_key_sample` — look for unexpectedly large or non-expiring keys.

## Remediation
- **No automatic remediation is whitelisted.** Cache flushes are explicitly
  denied by policy until a human signs off on which keys are safe to evict —
  flushing a warm cache can turn a latency blip into a DB stampede.

## Escalate when
- Always — attach the INFO snapshot, slowlog, and key sample to the
  escalation so the on-call can decide between eviction, memory bump, or a
  client-side fix.
