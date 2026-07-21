# Autonomous SRE Agent (APOE)

An AI agent that acts as an application-support / SRE engineer: it observes
live infrastructure, diagnoses incidents — deterministic rules for known
faults, an LLM investigation agent for novel ones — and executes only
safety-approved, idempotent remediations. Built for environments where
autonomy must be earned: default-deny policy, human-approval workflow,
append-only audit log, and an eval harness whose hard gate is **zero unsafe
actions, ever**.

**The result that matters** (from [`agent-orchestrator/evals/RESULTS.md`](agent-orchestrator/evals/RESULTS.md)):
on five novel faults no deterministic rule covers, the rules-only pipeline
diagnoses **0%** — it closes real incidents as healthy or mislabels them —
while the rules+LLM-investigator pipeline diagnoses them correctly and
escalates each one with cited evidence, with **0 unsafe actions** across
every run. The LLM proposes; the safety policy and idempotent engine dispose.

```
observe (agents, 10s timeouts, graceful degradation)
  → reason (pure rule engine → LLM ReAct investigator on UNKNOWN/low confidence)
    → knowledge first (SQLite FTS5: code, topology, runbooks, past incidents)
  → plan (default-deny YAML policy · allow / deny / approval_required)
  → act (idempotent remediation · append-only audit · one OTel trace per incident)
```

## Layout

| Directory | What |
|---|---|
| [`agent-orchestrator/`](agent-orchestrator/) | The agent: orchestrator, investigator, knowledge layer, safety policy, approvals API, eval harness. **Start with its README.** |
| [`enterprise-lab/`](enterprise-lab/) | Synthetic financial microservices lab (postgres, redis, kafka, three services, nginx, OTel/Prometheus) + chaos injector with 8 fault scenarios — the test target. |

## Quickstart

```bash
# Prove the thesis offline — no Docker, no API key, ~10 seconds
cd agent-orchestrator
pip install -r requirements.txt
python evals/run_evals.py --fake-llm

# Full live demo (Docker): see agent-orchestrator/DEMO.md — 5 minutes
```

Works air-gapped: local models via any OpenAI-compatible endpoint (Ollama,
vLLM), knowledge layer is local SQLite, no SaaS anywhere. See
[`agent-orchestrator/SECURITY.md`](agent-orchestrator/SECURITY.md) for the
threat model and per-provider data-egress analysis.

## License

MIT — see [LICENSE](LICENSE).
