# APOE 5-Minute Demo

From clone to watching the agent resolve incidents. Prereqs: Docker, Python 3.11+.

## Minute 0–1: prove the thesis offline (no Docker, no API key)

```bash
cd apoe-monorepo-V1/agent-orchestrator
pip install -r requirements.txt
python evals/run_evals.py --fake-llm
```

Read the table it prints (full report in `evals/RESULTS.md`): on five faults
no rule covers, **rules-only scores 0%** — it closes real incidents as
healthy or mislabels them — while **rules+investigator scores 100% with
zero unsafe actions**. That contrast is the project.

```bash
pytest    # 69 tests, ~6s, no infrastructure needed
```

## Minute 1–3: bring up the lab and watch a full autonomous incident

```bash
export APOE_API_KEY=demo-secret
cd ../enterprise-lab
docker compose up --build -d          # postgres, redis, kafka, 3 services,
                                      # telemetry, chaos-injector, APOE
docker compose ps                     # wait until services are healthy
```

Inject a Postgres lock storm and let APOE handle it end to end:

```bash
curl -X POST -H "X-API-Key: demo-secret" http://localhost:8085/simulate/db-lock
```

In the JSON response, follow the story:
1. `findings` — the db-lock agent reports FAULTED with the blocking pid;
2. `diagnosis` — rule `db-lock-contention` fires at confidence 0.95;
3. `verdicts` — the safety policy allows `terminate_blocking_queries`
   (confidence floor 0.9);
4. `results` — `pg_terminate_backend(pid)` applied, idempotently;
5. `state` — `resolved`. One incident, one OTel trace.

## Minute 3–4: a novel fault the rules cannot see

```bash
curl -X POST -H "X-API-Key: demo-secret" http://localhost:8085/simulate/high-cpu
```

Watch graceful degradation (the lab exports no Prometheus process metrics,
so the CPU agent reports a *degraded* finding rather than crashing). Then
check the audit trail and approvals surface:

```bash
docker exec apoe-agent-orchestrator cat /data/apoe_audit.jsonl
curl http://localhost:8085/approvals
```

To see the LLM investigator live, set `APOE_LLM_MODEL` (and provider/key or
a local Ollama base-url) in your environment, `docker compose up -d
agent-orchestrator` again, then trigger a novel fault:

```bash
curl -X POST -H "X-API-Key: demo-secret" http://localhost:9999/chaos/pool-exhaustion
curl -X POST -H "X-API-Key: demo-secret" http://localhost:8085/incidents \
     -d '{"trigger":"demo"}' -H 'content-type: application/json'
```

The diagnosis cites which read-only tool produced each piece of evidence,
and the incident **escalates with a written rationale** — none of the novel
faults has a whitelisted safe action.

## Minute 4–5: the governance surface

```bash
# Mutating endpoints without the key -> 401
curl -X POST http://localhost:8085/incidents -d '{}' -H 'content-type: application/json'

# Reject a queued approval with a recorded reason (if one is pending)
curl -X POST -H "X-API-Key: demo-secret" \
     http://localhost:8085/approvals/<id>/reject \
     -d '{"reason":"not during trading hours","actor":"sre-jane"}' \
     -H 'content-type: application/json'
```

Every one of those decisions is a line in the audit log. Clean up:

```bash
curl -X POST http://localhost:9999/chaos/reset
docker compose down
```
