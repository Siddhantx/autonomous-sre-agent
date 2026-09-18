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
             │      │         │ │ 16 read-only tools   │ │      ▼required  │     │
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

## GenAI evaluation layer (`evals/llm_eval/`)

The harness above proves the *pipeline*. This layer evaluates the *model* —
and is careful to keep the two apart, because conflating them is how an
architecture ceiling gets quoted as a capability score.

### What is measured, and what each number means

**Deterministic tier** — arithmetic only, no judge, no network, no spend.
These gate CI:

| Metric | What it catches |
|---|---|
| `root_cause_accuracy` | wrong diagnosis |
| `escalation_correctness` | escalating a fixable incident, or resolving one it should have escalated |
| `unsafe_actions` | **the hard gate** — anything executed without an allowing safety verdict |
| `safe_action_compliance` | acting outside the actions a scenario legitimately permits |
| `tool_selection_recall` | missing the decisive tool (extra probing is not penalised) |
| `tool_call_validity` | malformed arguments or hallucinated tool names |
| `fabrication` | inventing a root cause where the truth is "nothing is wrong" or "cannot tell" |

Plus a per-step latency profile, reported rather than pass/failed.

`fabrication` is a judge-free hallucination signal: on scenarios whose ground
truth is `unknown`, asserting a confident specific cause *is* fabrication by
construction. Hedged low-confidence guesses are deliberately **not** penalised
— honest uncertainty is the behaviour we want.

**Judged tier** — DeepEval: faithfulness, answer relevancy, hallucination, and
a custom GEval reasoning-quality rubric. Opt-in, and separate by necessity:
pip refuses DeepEval alongside this project's pinned telemetry stack
(`ResolutionImpossible` against `opentelemetry-api==1.23.0`), so it has its
own environment (`requirements-eval.txt`) and its own CI job. Faithfulness is
scored against what the agent *actually saw* — seed findings plus every tool
result — not against ground truth, because the question is whether the
conclusion follows from the available evidence, not whether it got lucky.

> **Judge-quality caveat.** The default judge is a small local model, chosen so
> the tier costs nothing and runs air-gapped. A 3B judge scoring a 3B agent is
> a weak judge. Treat judged scores as directional, not authoritative; point
> `build_judge` at a stronger model to strengthen them.

### The benchmark

`evals/datasets/faults.yaml` — 15 scenarios, versioned as data so a change to
what we measure shows up as a diff rather than buried in a test refactor.
Validated on load against the real enums: a misspelled root cause is a
load-time error, not a silent permanent mis-score. Three groups: `novel`
(no rule covers them), `known` (rules do, one with a legitimate safe action,
so "always escalate" cannot score well), and `adversarial` (healthy noise,
a misleading primary signal, blind observability, a red-herring deploy
correlation, two simultaneous faults).

### Red-team suite

`evals/datasets/redteam.yaml` — nine attacks assuming the model is already
compromised. It injects hostile output directly rather than trying to elicit
it, because for an agent whose premise is an untrusted LLM, the only question
that matters is whether anything it emits survives the gate. Covers whitelist
escape (shell, SQL, case-variants), prompt injection via tool output, gate
pressure (inflated confidence, action floods), fake operator approval, and
hallucination. **This one fails the build.**

### Running it

```bash
python evals/llm_eval/run.py              # baseline: free, offline, no model
python evals/llm_eval/run.py --redteam    # adversarial suite (CI gate)
python evals/llm_eval/run.py --model qwen2.5:3b   # real capability
```

Accuracy is *reported*, never gated. A weak model should produce a low score
and a green build: what CI must protect is the safety invariant, not
cleverness. Gating on accuracy only pressures people to tune the benchmark
until it passes.

### Current baseline (rules-only, no LLM)

| Group | root-cause | escalation | unsafe | safe-action | fabrication |
|---|---|---|---|---|---|
| novel | 0% | 60% | 100% | 100% | 100% |
| known | 20% | 60% | 100% | 100% | 100% |
| adversarial | 40% | 40% | 100% | 80% | 100% |

The 0% on novel faults reproduces, through an independent code path, the same
rules-only baseline `evals/RESULTS.md` reports. It is the honest starting line
for a model to beat — not a defect.

## Module map

| Module | Responsibility |
|---|---|
| `config.py` | Env-driven settings (`APOE_` prefix). No hardcoded secrets. |
| `models.py` | Every cross-boundary payload as pydantic v2 + enums. |
| `blackboard.py` | Incident state machine (validated transitions) + findings. |
| `agents.py` | Diagnostic agents: per-agent timeout, graceful degradation. |
| `reasoner.py` | Pure rule engine — the deterministic fast path. |
| `investigator.py` | LLM ReAct loop: budgets, 16 read-only tools (postgres, redis, kafka, prometheus, **logs via Loki**, **recent changes**, code search, knowledge), provider-agnostic client (Anthropic / any OpenAI-compatible / local). Recent changes are injected into the first prompt deterministically — "what changed?" is never left to the model to ask. |
| `knowledge/` | SQLite FTS5 store: lab code, topology, runbooks, incident post-mortems, **change events** (deploys/config/schema via `POST /changes` webhook + git-history ingestion). Searched before live infra; learns from every incident. |
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
