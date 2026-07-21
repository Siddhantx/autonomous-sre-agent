"""LLM investigation agent — the fallback when the rule engine can't diagnose.

ReAct loop under hard budgets (steps, tokens, wall-clock). The LLM sees the
incident findings and a whitelist of **read-only** tools; it replies in a
strict JSON protocol — either a batch of tool calls (fanned out in parallel)
or a final diagnosis. The output is the existing :class:`Diagnosis` model:

* evidence entries cite the tool call that produced them,
* proposed actions must map to the :class:`ActionType` enum — anything else
  is dropped and noted, and the result still passes the safety policy,
* the LLM never authors SQL/shell; tools run fixed queries written here.

The LLM client is a one-method protocol with two httpx-based implementations
(Anthropic Messages API, OpenAI-compatible chat completions) — no provider
SDK anywhere.
"""

from __future__ import annotations

import asyncio
import json
import re
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Protocol

from typing import TYPE_CHECKING

import httpx
from pydantic import BaseModel

from .blackboard import Blackboard
from .config import Settings
from .knowledge import KnowledgeStore
from .models import (
    ActionType,
    Diagnosis,
    Hypothesis,
    IncidentSession,
    ProposedAction,
    RootCause,
)
from .observability import get_logger, get_tracer

if TYPE_CHECKING:
    from .connectors import Connectors

log = get_logger("investigator")

_RESULT_MAX_CHARS = 4000  # per tool result, protects the token budget


# ---------------------------------------------------------------------------
# LLM client protocol + implementations
# ---------------------------------------------------------------------------
class LLMResponse(BaseModel):
    text: str
    tokens: int = 0


class LLMClient(Protocol):
    async def complete(
        self, messages: list[dict[str, str]], max_tokens: int
    ) -> LLMResponse: ...


class AnthropicClient:
    """Anthropic Messages API via raw httpx — no SDK."""

    def __init__(self, settings: Settings, client: httpx.AsyncClient | None = None):
        self._model = settings.llm_model
        self._client = client or httpx.AsyncClient(
            base_url=settings.llm_base_url or "https://api.anthropic.com",
            headers={
                "x-api-key": settings.llm_api_key,
                "anthropic-version": "2023-06-01",
            },
            timeout=30.0,
        )

    async def complete(
        self, messages: list[dict[str, str]], max_tokens: int
    ) -> LLMResponse:
        system = "\n".join(m["content"] for m in messages if m["role"] == "system")
        chat = [m for m in messages if m["role"] != "system"]
        resp = await self._client.post(
            "/v1/messages",
            json={
                "model": self._model,
                "system": system,
                "messages": chat,
                "max_tokens": max_tokens,
            },
        )
        resp.raise_for_status()
        body = resp.json()
        usage = body.get("usage", {})
        return LLMResponse(
            text="".join(b.get("text", "") for b in body.get("content", [])),
            tokens=usage.get("input_tokens", 0) + usage.get("output_tokens", 0),
        )


class OpenAICompatibleClient:
    """OpenAI-compatible chat completions (OpenAI, Ollama, vLLM) via httpx."""

    def __init__(self, settings: Settings, client: httpx.AsyncClient | None = None):
        self._model = settings.llm_model
        headers = {}
        if settings.llm_api_key:
            headers["Authorization"] = f"Bearer {settings.llm_api_key}"
        self._client = client or httpx.AsyncClient(
            base_url=settings.llm_base_url, headers=headers, timeout=30.0
        )

    async def complete(
        self, messages: list[dict[str, str]], max_tokens: int
    ) -> LLMResponse:
        resp = await self._client.post(
            "/chat/completions",
            json={
                "model": self._model,
                "messages": messages,
                "max_tokens": max_tokens,
            },
        )
        resp.raise_for_status()
        body = resp.json()
        return LLMResponse(
            text=body["choices"][0]["message"]["content"],
            tokens=body.get("usage", {}).get("total_tokens", 0),
        )


def make_llm_client(settings: Settings) -> LLMClient:
    if settings.llm_provider == "anthropic":
        return AnthropicClient(settings)
    if settings.llm_provider == "openai":
        return OpenAICompatibleClient(settings)
    raise ValueError(f"unknown llm_provider: {settings.llm_provider}")


# ---------------------------------------------------------------------------
# Read-only tools. SQL is authored HERE, never by the LLM.
# ---------------------------------------------------------------------------
async def _pg_stat_activity(args: dict[str, Any], ctx: ToolContext) -> Any:
    return await ctx.connectors.postgres.fetch(
        """
        SELECT pid, state, wait_event_type, wait_event,
               now() - query_start AS query_age, left(query, 200) AS query
        FROM pg_stat_activity
        WHERE state IS NOT NULL AND pid <> pg_backend_pid()
        ORDER BY query_start
        LIMIT 25
        """
    )


async def _pg_blocking(args: dict[str, Any], ctx: ToolContext) -> Any:
    return await ctx.connectors.postgres.blocking_backends()


async def _pg_table_stats(args: dict[str, Any], ctx: ToolContext) -> Any:
    return await ctx.connectors.postgres.fetch(
        """
        SELECT relname, seq_scan, idx_scan, n_live_tup, n_dead_tup,
               last_autovacuum
        FROM pg_stat_user_tables
        ORDER BY seq_scan DESC
        LIMIT 25
        """
    )


async def _pg_explain(args: dict[str, Any], ctx: ToolContext) -> Any:
    query = str(args.get("query", "")).strip()
    if ";" in query or not query.lower().startswith(("select", "with")):
        return {"error": "only single SELECT/WITH statements may be explained"}
    return await ctx.connectors.postgres.fetch(f"EXPLAIN (FORMAT JSON) {query}")


async def _redis_info(args: dict[str, Any], ctx: ToolContext) -> Any:
    return await ctx.connectors.redis.info()


async def _redis_slowlog(args: dict[str, Any], ctx: ToolContext) -> Any:
    return await ctx.connectors.redis.slowlog(25)


async def _redis_key_sample(args: dict[str, Any], ctx: ToolContext) -> Any:
    return await ctx.connectors.redis.key_sample(20)


async def _kafka_consumer_lag(args: dict[str, Any], ctx: ToolContext) -> Any:
    group = str(args.get("group_id", "payment-processors"))
    topic = str(args.get("topic", "order-events"))
    lag = await ctx.connectors.kafka.total_consumer_lag(group, topic)
    return {"group_id": group, "topic": topic, "lag": lag}


async def _kafka_topic_desc(args: dict[str, Any], ctx: ToolContext) -> Any:
    return await ctx.connectors.kafka.topic_offsets(
        str(args.get("topic", "order-events"))
    )


async def _prometheus_query(args: dict[str, Any], ctx: ToolContext) -> Any:
    promql = str(args.get("query", ""))
    return {"query": promql, "value": await ctx.connectors.prometheus.instant_query(promql)}


async def _prometheus_range(args: dict[str, Any], ctx: ToolContext) -> Any:
    return await ctx.connectors.prometheus.range_query(
        str(args.get("query", "")),
        str(args.get("start", "")),
        str(args.get("end", "")),
        str(args.get("step", "60s")),
    )


async def _code_search(args: dict[str, Any], ctx: ToolContext) -> Any:
    """Grep the lab service sources. stdlib only; small tree, no index needed."""
    pattern = str(args.get("pattern", "")).lower()
    if not pattern:
        return {"error": "pattern required"}
    matches: list[str] = []
    for path in sorted(ctx.settings.lab_source_path.rglob("*")):
        if not path.is_file() or path.stat().st_size > 200_000:
            continue
        if path.suffix not in {".py", ".js", ".ts", ".yml", ".yaml", ".sql", ".conf", ".env"}:
            continue
        try:
            for i, line in enumerate(path.read_text(encoding="utf-8", errors="ignore").splitlines(), 1):
                if pattern in line.lower():
                    rel = path.relative_to(ctx.settings.lab_source_path)
                    matches.append(f"{rel}:{i}: {line.strip()[:200]}")
                    if len(matches) >= 20:
                        return matches
        except OSError:
            continue
    return matches or {"info": "no matches"}


async def _log_search(args: dict[str, Any], ctx: ToolContext) -> Any:
    """Search recent container logs. LogQL is assembled HERE, never by the LLM."""
    service = re.sub(r"[^a-zA-Z0-9_-]", "", str(args.get("service", "")))
    pattern = str(args.get("pattern", ""))
    minutes = min(max(int(args.get("minutes", 15)), 1), 240)
    selector = f'{{container=~".*{service}.*"}}' if service else '{container=~".+"}'
    logql = selector
    if pattern:
        logql += f" |= {json.dumps(pattern)}"  # json escaping == LogQL string escaping
    return await ctx.connectors.loki.search(logql, minutes, limit=50)


async def _k8s_pod_states(args: dict[str, Any], ctx: ToolContext) -> Any:
    k8s = getattr(ctx.connectors, "k8s", None)
    if k8s is None:
        return {"error": "kubernetes not configured"}
    return await k8s.pod_states(str(args.get("namespace", "")) or None)


async def _k8s_events(args: dict[str, Any], ctx: ToolContext) -> Any:
    k8s = getattr(ctx.connectors, "k8s", None)
    if k8s is None:
        return {"error": "kubernetes not configured"}
    return await k8s.events(str(args.get("namespace", "")) or None)


async def _recent_changes(args: dict[str, Any], ctx: ToolContext) -> Any:
    if ctx.knowledge is None:
        return {"error": "knowledge store not configured"}
    service = str(args.get("service", "")) or None
    n = min(max(int(args.get("limit", 10)), 1), 20)
    return ctx.knowledge.recent_changes(n, service=service)


async def _knowledge_search(args: dict[str, Any], ctx: ToolContext) -> Any:
    if ctx.knowledge is None:
        return {"error": "knowledge store not configured"}
    query = str(args.get("query", ""))
    if not query:
        return {"error": "query required"}
    hits = ctx.knowledge.search(query, kind=args.get("kind"), limit=5)
    return [
        {"kind": h.kind, "ref": h.ref, "title": h.title, "snippet": h.snippet}
        for h in hits
    ]


async def _blackboard_context(args: dict[str, Any], ctx: ToolContext) -> Any:
    current = [
        {"agent": f.agent_name, "status": f.status.value, "summary": f.summary,
         "metrics": f.metrics}
        for f in ctx.session.findings
    ]
    past = [
        {"incident_id": s.incident_id, "state": s.state.value,
         "root_cause": s.diagnosis.root_cause.value if s.diagnosis else None}
        for s in ctx.blackboard.recent_sessions(5)
        if s.incident_id != ctx.session.incident_id
    ]
    return {"current_findings": current, "past_incidents": past}


@dataclass(frozen=True)
class ToolContext:
    """Everything a tool may read. Nothing it may write."""

    connectors: Connectors
    session: IncidentSession
    blackboard: Blackboard
    settings: Settings
    knowledge: KnowledgeStore | None = None


ToolHandler = Callable[[dict[str, Any], "ToolContext"], Awaitable[Any]]

# Tool-provider plugin layer: each provider groups the read-only tools for one
# backend interface (SQL via read-only DSN, Prometheus API, Loki API, ...).
# App-specific knowledge comes from ingestion, never from code changes here.
PROVIDERS: dict[str, dict[str, ToolHandler]] = {
    "postgres": {
        "pg_stat_activity": _pg_stat_activity,
        "pg_blocking": _pg_blocking,
        "pg_table_stats": _pg_table_stats,
        "pg_explain": _pg_explain,
    },
    "redis": {
        "redis_info": _redis_info,
        "redis_slowlog": _redis_slowlog,
        "redis_key_sample": _redis_key_sample,
    },
    "kafka": {
        "kafka_consumer_lag": _kafka_consumer_lag,
        "kafka_topic_desc": _kafka_topic_desc,
    },
    "prometheus": {
        "prometheus_query": _prometheus_query,
        "prometheus_range": _prometheus_range,
    },
    "logs": {"log_search": _log_search},
    "kubernetes": {
        "k8s_pod_states": _k8s_pod_states,
        "k8s_events": _k8s_events,
    },
    "code": {"code_search": _code_search},
    "knowledge": {
        "recent_changes": _recent_changes,
        "knowledge_search": _knowledge_search,
        "blackboard_context": _blackboard_context,
    },
}


def register_provider(name: str, tools: dict[str, ToolHandler]) -> None:
    """Register a third-party tool provider (e.g. a cloud-specific backend)."""
    PROVIDERS[name] = tools


def active_tools(settings: Settings) -> dict[str, ToolHandler]:
    """Merge the providers enabled by APOE_TOOL_PROVIDERS ('all' = every one)."""
    wanted = [p.strip() for p in settings.tool_providers.split(",") if p.strip()]
    names = list(PROVIDERS) if wanted == ["all"] else [
        p for p in wanted if p in PROVIDERS
    ]
    if not settings.k8s_api_url and "kubernetes" in names:
        names.remove("kubernetes")  # unconfigured backend: don't offer its tools
    merged: dict[str, ToolHandler] = {}
    for name in names:
        merged.update(PROVIDERS[name])
    return merged


# Backwards-compatible flat view of every registered tool.
TOOLS: dict[str, ToolHandler] = {}
for _provider_tools in PROVIDERS.values():
    TOOLS.update(_provider_tools)


async def _dispatch_tool(
    name: str,
    args: dict[str, Any],
    ctx: ToolContext,
    tools: dict[str, ToolHandler] | None = None,
) -> str:
    """Run one tool under a span; failures degrade to an error payload."""
    registry = tools if tools is not None else TOOLS
    tracer = get_tracer()
    with tracer.start_as_current_span(f"investigator.tool.{name}") as span:
        span.set_attribute("tool.name", name)
        started = time.perf_counter()
        try:
            handler = registry.get(name)
            if handler is None:
                result: Any = {"error": f"unknown tool '{name}'"}
            else:
                result = await handler(args, ctx)
        except Exception as exc:  # tool failures degrade, never crash the loop
            result = {"error": f"{type(exc).__name__}: {exc}"}
        payload = json.dumps(result, default=str)[:_RESULT_MAX_CHARS]
        log.info(
            "investigator_tool_call",
            tool=name,
            args=args,
            duration_ms=round((time.perf_counter() - started) * 1000, 1),
            result_chars=len(payload),
        )
        return payload


# ---------------------------------------------------------------------------
# Prompt + response parsing
# ---------------------------------------------------------------------------
_SYSTEM_PROMPT = """You are an SRE incident investigator for a financial \
microservices platform (postgres, redis, kafka, order/payment/inventory \
services behind nginx). Diagnose the root cause using READ-ONLY tools.

Reply with EXACTLY ONE JSON object, no prose, in one of two forms:

1. Request tools (you may batch several):
{"action": "tools", "calls": [{"tool": "<name>", "args": {...}}]}

Available tools: %s

2. Final diagnosis:
{"action": "diagnose", "root_cause": "<one of: %s>",
 "confidence": 0.0-1.0, "rationale": "...",
 "evidence": ["[tool_name] what it showed", ...],
 "proposed_actions": [{"action_type": "<one of: %s>",
                       "target": "...", "params": {}, "rationale": "..."}],
 "hypotheses": [{"root_cause": "...", "confidence": 0.0-1.0,
                 "evidence_for": [...], "evidence_against": [...]}]}

Work like a differential diagnosis: maintain 2-3 ranked hypotheses and
choose tool calls that DISCRIMINATE between them (a result that would
confirm one and rule out another beats one that confirms what you already
believe). Report the full differential in "hypotheses", including the
candidates you rejected and the evidence that killed them.

Rules:
- Most incidents follow a change. Weigh the recent changes in your context
  (and the recent_changes tool) BEFORE hunting for spontaneous infra faults.
- Consult knowledge_search (runbooks, topology, past incidents) BEFORE
  querying live infrastructure.
- Every evidence entry MUST cite the tool that produced it in [brackets].
- proposed_actions may ONLY use the listed action types. If none fits,
  return an empty list and explain in the rationale why a human must act.
- Prefer few, targeted tool calls; you have a hard step and token budget."""


def _system_prompt(tool_names: list[str] | None = None) -> str:
    return _SYSTEM_PROMPT % (
        ", ".join(tool_names if tool_names is not None else TOOLS),
        ", ".join(rc.value for rc in RootCause),
        ", ".join(at.value for at in ActionType),
    )


def _parse_json(text: str) -> dict[str, Any] | None:
    """Parse the model reply, tolerating markdown fences."""
    text = text.strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text.startswith("json"):
            text = text[4:]
    try:
        parsed = json.loads(text.strip())
        return parsed if isinstance(parsed, dict) else None
    except json.JSONDecodeError:
        return None


def _to_diagnosis(payload: dict[str, Any]) -> Diagnosis:
    """Map a diagnose payload onto the Diagnosis model, enforcing whitelists."""
    try:
        root_cause = RootCause(str(payload.get("root_cause", "")))
    except ValueError:
        root_cause = RootCause.UNKNOWN
    rationale = str(payload.get("rationale", ""))

    actions: list[ProposedAction] = []
    dropped: list[str] = []
    for raw in payload.get("proposed_actions", []) or []:
        try:
            actions.append(
                ProposedAction(
                    action_type=ActionType(str(raw.get("action_type", ""))),
                    target=str(raw.get("target", "")),
                    params=dict(raw.get("params", {}) or {}),
                    rationale=str(raw.get("rationale", "")),
                )
            )
        except ValueError:
            dropped.append(str(raw.get("action_type", "?")))
    if dropped:
        rationale += (
            f" [investigator: dropped non-whitelisted action(s): {', '.join(dropped)};"
            " escalating those to a human]"
        )

    hypotheses: list[Hypothesis] = []
    for raw in payload.get("hypotheses", []) or []:
        try:
            hypotheses.append(
                Hypothesis(
                    root_cause=RootCause(str(raw.get("root_cause", ""))),
                    confidence=min(max(float(raw.get("confidence", 0.0)), 0.0), 1.0),
                    evidence_for=[str(e) for e in raw.get("evidence_for", []) or []],
                    evidence_against=[
                        str(e) for e in raw.get("evidence_against", []) or []
                    ],
                )
            )
        except (ValueError, TypeError):
            continue  # unknown root cause or malformed entry: drop silently

    confidence = min(max(float(payload.get("confidence", 0.0)), 0.0), 1.0)
    return Diagnosis(
        root_cause=root_cause,
        confidence=confidence,
        rationale=rationale,
        evidence=[str(e) for e in payload.get("evidence", []) or []],
        proposed_actions=actions,
        hypotheses=hypotheses,
    )


def _escalation(reason: str, evidence: list[str]) -> Diagnosis:
    return Diagnosis(
        root_cause=RootCause.UNKNOWN,
        confidence=0.0,
        rationale=f"investigator escalation: {reason}",
        evidence=evidence,
        proposed_actions=[],
    )


# ---------------------------------------------------------------------------
# The ReAct loop
# ---------------------------------------------------------------------------
async def investigate(
    session: IncidentSession,
    blackboard: Blackboard,
    connectors: Connectors,
    settings: Settings,
    llm: LLMClient,
    knowledge: KnowledgeStore | None = None,
) -> Diagnosis:
    """Run the budgeted investigation loop. Never raises; escalates instead."""
    try:
        return await asyncio.wait_for(
            _loop(session, blackboard, connectors, settings, llm, knowledge),
            timeout=settings.investigator_timeout_s,
        )
    except asyncio.TimeoutError:
        log.warning("investigator_timeout", timeout_s=settings.investigator_timeout_s)
        return _escalation(
            f"timed out after {settings.investigator_timeout_s:.0f}s", []
        )
    except Exception as exc:
        log.error("investigator_error", error=str(exc), error_type=type(exc).__name__)
        return _escalation(f"{type(exc).__name__}: {exc}", [])


async def _loop(
    session: IncidentSession,
    blackboard: Blackboard,
    connectors: Connectors,
    settings: Settings,
    llm: LLMClient,
    knowledge: KnowledgeStore | None = None,
) -> Diagnosis:
    ctx = ToolContext(connectors, session, blackboard, settings, knowledge)
    findings_json = json.dumps(
        [
            {"agent": f.agent_name, "status": f.status.value, "summary": f.summary,
             "metrics": f.metrics, "degraded": f.degraded}
            for f in session.findings
        ],
        default=str,
    )
    # Knowledge first, deterministically: seed the context with runbook /
    # past-incident hits before the LLM can touch live infrastructure.
    knowledge_block = ""
    if knowledge is not None:
        seed = " ".join(f.summary for f in session.findings if f.is_fault) or "incident"
        hits = knowledge.search(seed, limit=5)
        if hits:
            knowledge_block = "\n\nKnowledge base (pre-searched):\n" + "\n".join(
                f"- [{h.kind}] {h.title} ({h.ref}): {h.snippet}" for h in hits
            )
        # Outcome-aware: what resolved similar incidents before beats prose.
        similar = knowledge.similar_incidents(seed, n=3)
        if similar:
            knowledge_block += (
                "\n\nSimilar past incidents and their outcomes:\n" + "\n".join(
                    f"- {r.get('root_cause', '?')} -> {r.get('final_state', '?')}"
                    + (
                        "; actions: " + ", ".join(
                            f"{a['action']}={a['status']}" for a in r["actions"]
                        )
                        if r.get("actions")
                        else "; no actions taken"
                    )
                    for r in similar
                )
            )
        # "What changed?" — always injected, never left to the model to ask.
        changes = knowledge.recent_changes(5)
        if changes:
            knowledge_block += "\n\nRecent changes (newest first):\n" + "\n".join(
                f"- {c['at']} [{c['change_kind']}] {c['service']}: "
                f"{c['summary']} (by {c['actor']})"
                for c in changes
            )
    if settings.swarm_enabled:
        from .swarm import swarm_investigate
        return await swarm_investigate(
            ctx, llm, findings_json, knowledge_block, settings,
        )

    tools = active_tools(settings)
    messages: list[dict[str, str]] = [
        {"role": "system", "content": _system_prompt(list(tools))},
        {"role": "user",
         "content": f"Incident findings:\n{findings_json}{knowledge_block}"},
    ]
    tokens_used = 0
    tracer = get_tracer()

    for step in range(settings.investigator_max_steps):
        remaining = settings.investigator_max_tokens - tokens_used
        if remaining <= 0:
            break
        with tracer.start_as_current_span("investigator.llm") as span:
            span.set_attribute("investigator.step", step)
            response = await llm.complete(messages, max_tokens=remaining)
            span.set_attribute("investigator.tokens", response.tokens)
        tokens_used += response.tokens
        log.info(
            "investigator_llm_call", step=step, tokens=response.tokens,
            tokens_used=tokens_used,
        )

        payload = _parse_json(response.text)
        if payload is None or payload.get("action") not in ("tools", "diagnose"):
            messages.append({"role": "assistant", "content": response.text})
            messages.append(
                {"role": "user",
                 "content": "Invalid reply. Respond with exactly one JSON object "
                            "using the documented protocol."}
            )
            continue

        if payload["action"] == "diagnose":
            diagnosis = _to_diagnosis(payload)
            log.info(
                "investigator_diagnosed",
                root_cause=diagnosis.root_cause.value,
                confidence=diagnosis.confidence,
                steps=step + 1,
                tokens_used=tokens_used,
            )
            return diagnosis

        calls = payload.get("calls", []) or []
        # Parallel fan-out over the requested tools (swarm-orchestration pattern).
        results = await asyncio.gather(
            *(
                _dispatch_tool(
                    str(c.get("tool", "")), dict(c.get("args", {}) or {}), ctx, tools
                )
                for c in calls
            )
        )
        messages.append({"role": "assistant", "content": response.text})
        messages.append(
            {"role": "user",
             "content": "\n".join(
                 f"[{c.get('tool', '?')}] {r}" for c, r in zip(calls, results)
             ) or "no tool calls provided"}
        )

    log.warning(
        "investigator_budget_exhausted",
        tokens_used=tokens_used,
        max_steps=settings.investigator_max_steps,
    )
    return _escalation(
        f"budget exhausted (steps={settings.investigator_max_steps}, "
        f"tokens={tokens_used})",
        [],
    )
