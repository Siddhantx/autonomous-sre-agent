# APOE — Active Agent Orchestrator (V1)

The central "brain" of the Autonomous AI Production Operations Engineer. When an
incident is triggered, the orchestrator **observes** the running enterprise-lab
subsystems with a fleet of diagnostic agents, **reasons** about the root cause,
checks a **safety policy**, and executes **idempotent remediation** — emitting a
single distributed trace and structured JSON logs for every step.

## Architecture

Blackboard-based multi-agent control loop. One incident = one `IncidentSession`
record on the blackboard, driven through an explicit state machine:

```
CREATED → DIAGNOSING → DIAGNOSED → PLANNING → REMEDIATING → RESOLVED
                            │           │            │
                            └──────────►└───────────►└──────► ESCALATED / FAILED
```

```
              ┌─────────────────────────  Orchestrator  ─────────────────────────┐
              │                                                                    │
  trigger ──► │  1. observe        2. reason        3. plan          4. remediate │
              │  ┌──────────┐      ┌─────────┐     ┌──────────┐     ┌───────────┐ │
              │  │ agents   │────► │ reasoner│───► │ safety   │───► │remediation│ │
              │  │ (gather, │ find │ (pure   │ dx  │ policy   │ ok  │ engine    │ │
              │  │  10s TO) │ ings │  rules) │     │ compiler │     │(idempotent)│ │
              │  └────┬─────┘      └─────────┘     └──────────┘     └─────┬─────┘ │
              │       │                 ▲                                 │       │
              │       ▼                 │            Blackboard           ▼       │
              │  connectors  ◄──────────┴──── (state machine + findings) ────►    │
              └───────┼────────────────────────────────────────────────────────┘
                      ▼
        Prometheus · Postgres · Redis · Kafka · chaos-injector
```

### Module responsibilities

| Module | Responsibility |
|---|---|
| `config.py` | Env-driven `pydantic-settings`. No hardcoded secrets. |
| `observability.py` | `structlog` JSON logging (every record carries `incident_id`, `agent_name`, `timestamp`, `severity`) + OpenTelemetry traces & metrics. |
| `models.py` | All cross-boundary payloads as `pydantic` v2 models + enums. |
| `blackboard.py` | Shared incident state, **validated** state-machine transitions, concurrency-safe finding appends, idempotency-key tracking. |
| `connectors.py` | Async, `tenacity` exponential-backoff clients for Prometheus / Postgres / Redis / Kafka / chaos-injector. |
| `agents.py` | Diagnostic agents. Per-agent `asyncio.wait_for` timeout; **graceful degradation** (a failed scrape → `degraded` finding, never a crash). |
| `reasoner.py` | Pure, deterministic rule engine: findings → `Diagnosis` (root cause + confidence + proposed actions). |
| `safety.py` | Compiles the declarative YAML safety policy into predicates; **default-deny**, ordered first-match, confidence-gated. |
| `remediation.py` | Executes only safety-approved actions. Replay-safe at session level (idempotency keys) and action level (idempotent operations). |
| `orchestrator.py` | The pipeline; one distributed trace per incident. |
| `main.py` | FastAPI: trigger incidents and run the end-to-end chaos simulation. |
| `policies.yaml` | The versioned, human-auditable safety policy. |

### Design guarantees (mapped to the engineering bar)

- **Type safety** — `pydantic` v2 everywhere; no untyped payloads on the critical path.
- **Structured logging** — `structlog` JSON; mandated fields injected by processors + contextvars.
- **Async first** — agents fan out via `asyncio.gather`, each under a 10s `wait_for`.
- **Resilience** — every external call retries with `tenacity`; agents degrade, they never crash the run; an incident-level exception fails only that incident.
- **Idempotency** — session-level idempotency keys + action-level idempotent operations (`pg_terminate_backend`, chaos `reset`, `DEL`).
- **Safety** — default-deny policy compiler; low-confidence diagnoses cannot fire destructive actions.
- **Observability** — OpenTelemetry traces + metrics; every incident is one trace.

## Running the tests

Critical modules (reasoner, safety compiler, blackboard) are pure — no live
infra needed:

```bash
cd agent-orchestrator
python -m venv .venv && . .venv/Scripts/activate      # Windows: .venv\Scripts\activate
pip install pydantic pydantic-settings PyYAML pytest pytest-asyncio pytest-cov
pytest --cov=agent_orchestrator.reasoner \
       --cov=agent_orchestrator.safety \
       --cov=agent_orchestrator.blackboard --cov-report=term-missing
```

Current coverage on those modules is **97%** (bar: >85%).

## End-to-end incident simulation

Bring up the whole lab (the orchestrator is wired into the compose file):

```bash
cd enterprise-lab
docker compose up --build
```

Then drive a full autonomous incident. The `db-lock` scenario is the reliable
end-to-end path (the DB-lock agent reads `pg_blocking_pids` directly):

```bash
# Inject a Postgres lock, wait for it to become observable, run the pipeline:
curl -X POST http://localhost:8085/simulate/db-lock
```

Expected sequence in the JSON response / logs:

1. chaos-injector holds `ACCESS EXCLUSIVE` on `orders`;
2. `db-lock-agent` reports `FAULTED` with the blocking pid;
3. reasoner → `db_lock_contention` (confidence 0.95), proposes
   `terminate_blocking_queries`;
4. safety policy allows it (confidence ≥ 0.9);
5. remediation calls `pg_terminate_backend(pid)` — the lock releases;
6. session transitions to `RESOLVED`.

Other endpoints:

```bash
curl -X POST http://localhost:8085/incidents -d '{"trigger":"manual"}' -H 'content-type: application/json'
curl     http://localhost:8085/incidents/<incident_id>
curl -X POST http://localhost:8085/simulate/high-cpu   # demonstrates graceful degradation (no Prometheus series)
```

> Note: the lab services export OTLP traces but not Prometheus `/metrics`, so the
> CPU/memory/kafka agents typically return **degraded** findings — an intentional
> demonstration of the resilience contract. The DB-lock path exercises the full
> observe→reason→act→resolve loop against real infrastructure.

## Configuration

All settings are `APOE_`-prefixed env vars — see [`.env.example`](.env.example).
Defaults match the lab compose network; override every value (including the
Postgres DSN) from the environment in production.
```
