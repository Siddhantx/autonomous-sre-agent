# Investigator Design — APOE V2 Phase 1

## Problem

The V1 rule engine returns `RootCause.UNKNOWN` (confidence 0.0) for any fault it has no
hand-written rule for. Novel faults — the ones that actually page on-call — fall through.
The investigator closes that gap: when rules fail or confidence is low, an LLM drives a
read-only investigation loop and returns a structured `Diagnosis`.

---

## Activation

Inside `Orchestrator._diagnose()`, immediately after `reason()`:

```
diagnosis = reason(findings)
if diagnosis.root_cause == UNKNOWN or diagnosis.confidence < APOE_INVESTIGATOR_THRESHOLD:
    diagnosis = await investigate(session, connectors, settings, llm)
blackboard.set_diagnosis(incident_id, diagnosis)
```

`APOE_INVESTIGATOR_THRESHOLD` defaults to `0.7`. The rule engine stays the fast path;
the investigator is the fallback.

---

## ReAct Loop

```
messages = [system_prompt, incident_context]
steps = 0
tokens_used = 0

while steps < max_steps and tokens_used < max_tokens:
    response = await llm.complete(messages, remaining_token_budget)
    tokens_used += response.tokens

    if response.is_final_answer:
        return parse_diagnosis(response.text)   # maps to Diagnosis model

    # Fan out all requested tool calls in parallel (swarm-orchestration pattern)
    tool_calls = parse_tool_calls(response)
    results = await asyncio.gather(*[dispatch_tool(tc, connectors, session) for tc in tool_calls])

    messages += [*tool_calls_as_messages, *results_as_messages]
    steps += 1

# Budget exhausted — escalate
return Diagnosis(root_cause=UNKNOWN, confidence=0.0,
                 rationale="investigator budget exhausted", ...)
```

Hard limits (all env-configurable):

| Env var | Default | Enforced via |
|---|---|---|
| `APOE_INVESTIGATOR_MAX_STEPS` | 8 | loop counter |
| `APOE_INVESTIGATOR_MAX_TOKENS` | 4000 | cumulative token count |
| `APOE_INVESTIGATOR_TIMEOUT_S` | 30 | `asyncio.wait_for` wrapping the whole loop |

---

## Read-Only Tool Set

| Tool name | Subsystem | What it calls |
|---|---|---|
| `pg_stat_activity` | Postgres | `pg_stat_activity` view |
| `pg_blocking` | Postgres | `pg_blocking_pids`, `pg_locks` |
| `pg_table_stats` | Postgres | `pg_stat_user_tables`, `pg_stat_user_indexes` |
| `pg_explain` | Postgres | `EXPLAIN (FORMAT JSON)` on a provided query |
| `redis_info` | Redis | `INFO all` |
| `redis_slowlog` | Redis | `SLOWLOG GET 25` |
| `redis_key_sample` | Redis | `SCAN` + `TYPE`/`TTL` on a sample |
| `kafka_consumer_lag` | Kafka | consumer group lag via existing `KafkaConnector` |
| `kafka_topic_desc` | Kafka | topic partition / offset info |
| `prometheus_query` | Prometheus | instant PromQL via existing `PrometheusConnector` |
| `prometheus_range` | Prometheus | range PromQL (start/end/step) |
| `code_search` | Codebase | `grep` under `enterprise-lab/` service sources |
| `blackboard_context` | Blackboard | current findings + last 5 incident summaries |

No DML, no destructive calls. Every tool call: one OTel child span + one structlog record.

---

## LLM Client

A minimal `Protocol` — one method, no coupling to any SDK:

```python
class LLMClient(Protocol):
    async def complete(
        self,
        messages: list[dict],
        max_tokens: int,
    ) -> LLMResponse: ...
```

Two concrete implementations, chosen at startup by `APOE_LLM_PROVIDER`:

| Provider | Implementation | Config |
|---|---|---|
| `anthropic` | `AnthropicClient` (uses `anthropic` SDK) | `APOE_LLM_MODEL`, `APOE_LLM_API_KEY` |
| `openai` | `OpenAICompatibleClient` (uses `httpx`, raw API) | `APOE_LLM_BASE_URL`, `APOE_LLM_MODEL`, `APOE_LLM_API_KEY` |

`openai` mode covers OpenAI, Ollama, vLLM, and any other OpenAI-compatible endpoint via
`APOE_LLM_BASE_URL`. The investigator logic never imports a provider SDK — it speaks only
to the `LLMClient` protocol. No lock-in.

New settings (all `APOE_`-prefixed, added to `config.py`):

```
APOE_LLM_PROVIDER          = "anthropic"
APOE_LLM_BASE_URL          = ""               # required for openai provider
APOE_LLM_MODEL             = ""               # e.g. claude-haiku-4-5-20251001
APOE_LLM_API_KEY           = ""               # secret; never logged
APOE_INVESTIGATOR_THRESHOLD    = 0.7
APOE_INVESTIGATOR_MAX_STEPS    = 8
APOE_INVESTIGATOR_MAX_TOKENS   = 4000
APOE_INVESTIGATOR_TIMEOUT_S    = 30.0
```

---

## Output Contract

The investigator returns the existing `Diagnosis` pydantic model — no new types.

- `evidence`: each entry cites the tool call that produced it (e.g.
  `"[pg_stat_activity] 3 idle-in-transaction sessions blocking orders table"`).
- `proposed_actions`: only `ActionType` enum members. If the LLM proposes an action that
  has no enum match, it is **silently dropped** and `rationale` records the escalation
  reason. The LLM never gets free-form shell/SQL execution.
- If no whitelisted action fits: `proposed_actions=[]`, clear `rationale` explaining why
  human review is needed. The orchestrator's existing no-action path then escalates.

Safety gating is unchanged — approved actions still pass through `safety.py` before
`remediation.py`. The investigator is advisory only.

---

## File Layout

```
agent_orchestrator/
  investigator.py        # ReAct loop, tool dispatch, LLM protocol + 2 clients (~250 lines)
  config.py              # +8 new APOE_ settings (no new file)
  orchestrator.py        # +5 lines: activation check after reason()
tests/
  test_investigator.py   # fake LLM: budget, whitelist, escalation, scripted tool sequence
```

No new dependencies for the `openai` client path (uses `httpx`, already present).
`anthropic` SDK is added only if `APOE_LLM_PROVIDER=anthropic` — gated behind a lazy
import so the test suite runs without it.

---

## What This Does Not Do

- No write operations, no DML, no destructive calls — ever.
- No free-form shell execution.
- No new database or external service.
- No changes to the `ActionType` enum or `safety.py` policy (Phase 3).
- No knowledge store (Phase 2).
