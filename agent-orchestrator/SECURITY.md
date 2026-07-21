# APOE Security Model

Audience: security reviewers at regulated institutions evaluating APOE for
on-prem deployment.

## Threat model — what the LLM can and cannot do

The LLM is treated as an **untrusted advisor** inside a trusted pipeline.

| The LLM can | The LLM cannot |
|---|---|
| Read findings the diagnostic agents already collected | Execute anything directly — it has no shell, no SQL, no network |
| Call 16 read-only tools (fixed queries authored in code — incl. log search whose LogQL is assembled in code; see below) | Author SQL — tools run queries written in `investigator.py`; the one exception, `pg_explain`, accepts only a single `SELECT`/`WITH` statement and wraps it in `EXPLAIN` (never `ANALYZE`) |
| Propose actions **by name** from the `ActionType` whitelist enum | Invent new action types — proposals outside the enum are dropped and recorded |
| Recommend confidence and rationale | Bypass the safety policy — every proposed action is evaluated against the default-deny YAML policy; `approval_required` actions wait for a human via the `/approvals` API |
| Search the local knowledge store | Write to the knowledge store, blackboard, or any subsystem |

Defense in depth, in order: (1) read-only tool set → (2) `ActionType`
whitelist at parse time → (3) default-deny policy with confidence floors →
(4) human-approval queue for gated actions → (5) idempotent execution
engine → (6) append-only audit log of every decision.

Prompt-injection stance: tool outputs (logs, DB rows, code snippets) are
untrusted content inside the LLM context. A poisoned tool output can at
worst make the LLM *propose* a bad action — layers 2–4 stop it from
executing. The hard eval gate (`evals/run_evals.py`) asserts zero executed
actions without an allowing verdict on every CI run.

## What data leaves the network, per provider

| `APOE_LLM_PROVIDER` | Endpoint | Data sent | Data retained off-site |
|---|---|---|---|
| *(unset model)* | — | Nothing. Investigator disabled; rules-only pipeline. | Nothing |
| `openai` + local `APOE_LLM_BASE_URL` (Ollama / vLLM) | Your host | Nothing leaves the network | Nothing |
| `anthropic` | api.anthropic.com | Incident findings, tool results (DB stats, redis INFO, kafka lag, log lines, lab code snippets), knowledge-store snippets | Per Anthropic's data-retention policy |
| `openai` + cloud base URL | That vendor | Same as above | Per that vendor's policy |

What is **never** sent to any provider: credentials and DSNs (config values
are not part of prompts), the API key, the audit log, raw customer data
(tools return operational metadata: pids, counts, latencies, schema names).
Caveat: `pg_stat_activity` includes query text and `code_search` includes
source lines — if your queries or code embed sensitive literals, use a local
model or the rules-only mode.

## Air-gapped deployment

1. **No LLM**: leave `APOE_LLM_MODEL` empty. The deterministic pipeline is
   fully functional; novel faults escalate to humans with collected findings.
2. **Local LLM** (recommended): run Ollama or vLLM inside the perimeter and
   set:
   ```
   APOE_LLM_PROVIDER=openai
   APOE_LLM_BASE_URL=http://<internal-host>:11434/v1
   APOE_LLM_MODEL=<local model tag>
   ```
   No SDK, no telemetry, no callbacks — the client is plain HTTP to the URL
   you configure.
3. The knowledge layer is SQLite + FTS5 on local disk. No embedding API, no
   SaaS, no network calls.
4. Images: mirror the pinned base images into your registry and pin by
   digest: `docker inspect --format='{{index .RepoDigests 0}}' <image>`,
   then replace `FROM`/`image:` entries with `@sha256:...` references.

## Operational controls

- **Auth**: every mutating endpoint requires `X-API-Key` matching
  `APOE_API_KEY` (constant-time compare). No key configured = all mutating
  requests rejected.
- **Audit**: `APOE_AUDIT_LOG_PATH` is an append-only JSONL of every
  proposed / approved / rejected / executed action with actor, timestamp,
  incident id and rationale. Ship it to your SIEM.
- **Dry run**: `APOE_DRY_RUN=true` runs the full pipeline without executing;
  the flag is stamped on every log record and every exported span
  (`apoe.dry_run`).
- **Containers**: non-root user, pinned versions, resource limits,
  healthchecks, graceful SIGTERM shutdown. The chaos-injector has **no**
  docker-socket access.
- **Secrets**: all config via `APOE_`-prefixed env vars; nothing hardcoded.
  The lab compose file contains sandbox credentials for the synthetic lab
  only.

## Reporting

This is a portfolio/lab project; report issues via the repository issue
tracker.
