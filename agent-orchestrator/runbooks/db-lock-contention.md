# Postgres lock contention (blocked backends)

## Symptoms
- Order/payment API latency spikes or timeouts; `orders` table writes hang.
- `db-lock-agent` reports FAULTED with `blocking_backends > 0`.
- `pg_stat_activity` shows sessions with `wait_event_type = 'Lock'`.

## Diagnosis
1. Run `pg_blocking` — identify the lead blocking pid and its query.
2. Check the blocker's `state`: `idle in transaction` means an application
   session forgot to commit; an `ACCESS EXCLUSIVE` holder is usually a DDL
   or the chaos-injector's db-lock scenario.
3. Confirm the blocked victims are application queries (order-service /
   payment-service DSNs), not autovacuum.

## Remediation
- `terminate_blocking_queries` on the lead blocking pid
  (`pg_terminate_backend`). Idempotent: a gone pid returns false.
- Safe when confidence >= 0.9 — the victim queries retry via the service
  connection pools.

## Escalate when
- The blocker is a long-running migration a human started deliberately.
- Terminating the lead blocker does not clear the queue within one minute
  (lock convoy — needs application-level fix).
