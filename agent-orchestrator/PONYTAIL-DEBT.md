# Ponytail Debt Ledger

Deliberate shortcuts marked with `# ponytail:` comments, harvested by
`/ponytail-debt`. Each entry names its ceiling and upgrade path — pay it
only when the ceiling is actually hit.

| # | Location | Shortcut | Ceiling | Upgrade path |
|---|---|---|---|---|
| 1 | `agent_orchestrator/blackboard.py:30` | Incident sessions in an in-process dict | Sessions lost on orchestrator restart | Persisted saga log if sessions must survive restarts |
| 2 | `agent_orchestrator/knowledge/store.py:34` | sqlite3 with `check_same_thread=False`, no lock | Only safe on a single asyncio event loop | Connection pool if the API grows worker threads |
| 3 | `agent_orchestrator/approvals.py:7` | In-memory approval queue | Pending approvals lost on restart (audit JSONL remains the durable record) | SQLite table if operators need restart-safe queues |
| 4 | `agent_orchestrator/audit.py:31` | Audit write failure logs but does not block the action | An action can execute without an audit line if the disk fails | Flip to fail-closed (refuse to act on audit failure) if compliance demands |

Related design-level deferrals (not `ponytail:`-marked, documented elsewhere):

- Image digest pinning is a deployment step (`SECURITY.md` § air-gapped) —
  exact version tags are pinned in-repo; digests depend on the mirror.
- `otel-collector` has no compose healthcheck (distroless image, no shell);
  container liveness is the check.
