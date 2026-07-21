# APOE — Autonomous Production Operations Engineer (V2)

The central "brain" of an AI application-support / SRE engineer. When an
incident triggers, APOE **observes** the running enterprise-lab subsystems,
**reasons** about the root cause — deterministic rules first, an LLM
investigation agent when rules can't explain what it sees — checks a
**default-deny safety policy** (with a human-approval queue for gated
actions), and executes **idempotent remediation**. Every step is one
distributed trace, structured JSON logs, and an append-only audit line.

## V2 architecture

```
 trigger ──► ┌────────────────────────── Orchestrator ──────────────────────────┐
             │                                                                   │
             │ 1. observe          2. reason                 3. plan   4. act    │
             │ ┌──────────┐   ┌──────────────────────────┐  ┌────────┐ ┌───────┐ │
             │ │ agents   │──►│ reasoner (5 pure rules)  │─►│ safety │►│remedi-│ │
             │ │ (10s TO, │   │      │ UNKNOWN /         │  │ policy │ │ation  │ │
             │ │ degrade) │   │      ▼ conf < 0.7        │  │(default│ │(idem- │ │
             │ └────┬─────┘   │ ┌──────────────────────┐ │  │ -deny) │ │potent)│ │
             │      │         │ │ INVESTIGATOR (LLM)   │ │  └───┬────┘ └───┬───┘ │
             │      │         │ │ ReAct loop, budgets, │ │      │approval  │     │
             │      │         │ │ 14 read-only tools   │ │      ▼required  │     │
             │      │         │ └──────────┬───────────┘ │  ┌────────┐    │     │
             │      │         └────────────│─────────────┘  │approval│    │     │
             │      ▼                      ▼                │ queue  │    ▼     │
             │  Blackboard (state machine + findings)       │ + API  │  audit   │
             │      ▲                      │                └────────┘  JSONL   │
             │      │                      ▼                                     │
             │      │         KNOWLEDGE STORE (SQLite FTS5)                      │
             │      │         code · topology · runbooks · past incidents        │
             │      │         (searched BEFORE live infra; post-mortem           │
             │      │          appended on every terminal incident)              │
             └──────┼────────────────────────────────────────────────────────────┘
                    ▼
       Prometheus · Postgres · Redis · Kafka · chaos-injector
```

The LLM **proposes only**: its actions must map onto the `ActionType`
whitelist enum and still pass the safety policy. There is no free-form
shell or SQL execution path anywhere.

## Quickstart

```bash
# 1. Bring up the lab + orchestrator (from enterprise-lab/)
export APOE_API_KEY=choose-a-secret          # required for mutating endpoints
cd enterprise-lab && docker compose up --build

# 2. Drive a rule-covered incident end to end
curl -X POST -H "X-API-Key: $APOE_API_KEY" http://localhost:8085/simulate/db-lock

# 3. Run the eval harness — one command, no API spend
cd ../agent-orchestrator && python evals/run_evals.py --fake-llm
```

To let the investigator use a real LLM, set `APOE_LLM_MODEL` (+ provider /
key / base-url) — see [`.env.example`](.env.example). Local models work via
any OpenAI-compatible endpoint (`APOE_LLM_PROVIDER=openai`,
`APOE_LLM_BASE_URL=http://localhost:11434/v1` for Ollama).

## The eval harness (proof of the thesis)

`evals/run_evals.py` injects five faults **no deterministic rule covers**
(connection-pool exhaustion, bad config deploy, slow-query regression,
kafka poison pill, disk fill) and scores rules-only vs rules+investigator
on root-cause accuracy, escalation correctness, time-to-diagnosis, and a
hard gate: **any executed action without an allowing safety verdict fails
the harness**. Results: [`evals/RESULTS.md`](evals/RESULTS.md).

| Mode | Command | Needs |
|---|---|---|
| Offline / CI (scripted LLM) | `python evals/run_evals.py --fake-llm` | nothing |
| Real local model, offline | `python evals/run_evals.py --ollama <model>` | Ollama |
| Live lab | `python evals/run_evals.py --live` | docker lab + `APOE_LLM_*` |

Measured results: [`evals/RESULTS.md`](evals/RESULTS.md) (scripted — the
architecture ceiling and CI gate) and
[`evals/RESULTS-local-llm.md`](evals/RESULTS-local-llm.md) (qwen2.5:3b on an
8GB CPU-only laptop: 27% root-cause accuracy vs the 0% rules baseline, 60%
correct escalation, **0 unsafe actions in 30 runs**).

## Module map

| Module | Responsibility |
|---|---|
| `config.py` | Env-driven settings (`APOE_` prefix). No hardcoded secrets. |
| `models.py` | Every cross-boundary payload as pydantic v2 + enums. |
| `blackboard.py` | Incident state machine (validated transitions) + findings. |
| `agents.py` | Diagnostic agents: per-agent timeout, graceful degradation. |
| `reasoner.py` | Pure rule engine — the deterministic fast path. |
| `investigator.py` | LLM ReAct loop: budgets, 14 read-only tools, provider-agnostic client (Anthropic / any OpenAI-compatible / local). |
| `knowledge/` | SQLite FTS5 store: lab code, topology, runbooks, incident post-mortems. Searched before live infra; learns from every incident. |
| `safety.py` | Declarative YAML policy compiler: allow / deny / approval_required, confidence-gated, default-deny. |
| `approvals.py` | Human-approval queue for gated actions. |
| `remediation.py` | Idempotent execution engine (session + action level). |
| `audit.py` | Append-only JSONL audit log of every action decision. |
| `orchestrator.py` | The pipeline; one OTel trace per incident. |
| `main.py` | FastAPI: incidents, simulation, approvals. API-key auth on all mutating endpoints. |

## Tests

```bash
pip install -r requirements.txt && pytest        # 69 tests, no live infra needed
```

Coverage bar ≥85% on pure modules (currently: reasoner 96%, safety 100%,
blackboard 100%, investigator 98%, knowledge 98–100%, approvals/audit 100%).
CI (`.github/workflows/ci.yml`) runs ruff, mypy `--strict`, the test suite
with the coverage gate, and the eval harness in fake-llm mode.

## Security & operations

See [`SECURITY.md`](SECURITY.md) for the threat model, what data leaves the
network per LLM provider choice, and air-gapped deployment. See
[`DEMO.md`](DEMO.md) for a 5-minute reviewer walkthrough.
