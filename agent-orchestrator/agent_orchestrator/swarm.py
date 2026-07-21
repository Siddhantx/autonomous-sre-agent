"""Multi-agent swarm investigation — parallel specialist investigators.

When APOE_SWARM_ENABLED=true (default: false), the investigator fans out to
specialist sub-investigators, each with a subset of tools (DB, messaging,
infra, app). Their independent diagnoses are synthesized into a single
Diagnosis by a lightweight merge pass.

# ponytail: sequential LLM calls per specialist, not truly concurrent LLM
# sessions. True multi-model concurrency when token throughput matters.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass

from .config import Settings
from .investigator import (
    LLMClient,
    ToolContext,
    _dispatch_tool,
    _parse_json,
    _to_diagnosis,
    active_tools,
)
from .models import Diagnosis, Hypothesis, RootCause
from .observability import get_logger

log = get_logger("swarm")


@dataclass(frozen=True)
class Specialist:
    name: str
    providers: list[str]
    focus: str


SPECIALISTS = [
    Specialist("db-specialist", ["postgres", "knowledge"], "database"),
    Specialist("messaging-specialist", ["kafka", "logs", "knowledge"], "messaging/kafka"),
    Specialist("infra-specialist", ["prometheus", "kubernetes", "knowledge"], "infrastructure/metrics"),
    Specialist("app-specialist", ["code", "logs", "redis", "knowledge"], "application code/config"),
]


_SPECIALIST_PROMPT = """You are a specialist SRE investigator focused on {focus}.
You have access ONLY to: {tool_names}

Given incident findings, use your tools to investigate from YOUR specialist
perspective only. Reply with exactly one JSON object:

1. Tool calls: {{"action": "tools", "calls": [{{"tool": "...", "args": {{}}}}]}}
2. Diagnosis: {{"action": "diagnose", "root_cause": "<one of: {root_causes}>",
   "confidence": 0.0-1.0, "rationale": "...",
   "evidence": ["[tool] what it showed"],
   "hypotheses": [{{"root_cause": "...", "confidence": 0.0-1.0,
                    "evidence_for": [...], "evidence_against": [...]}}]}}

You have a maximum of {max_steps} steps. If your domain shows nothing
relevant, diagnose with root_cause="unknown" and confidence=0.0."""


async def _run_specialist(
    specialist: Specialist,
    ctx: ToolContext,
    llm: LLMClient,
    findings_json: str,
    knowledge_block: str,
    settings: Settings,
) -> Diagnosis:
    """Run one specialist's investigation loop (budgeted)."""
    provider_tools = {}
    all_tools = active_tools(settings)
    for name, handler in all_tools.items():
        for prov in specialist.providers:
            from .investigator import PROVIDERS
            if name in PROVIDERS.get(prov, {}):
                provider_tools[name] = handler
                break

    if not provider_tools:
        return Diagnosis(
            root_cause=RootCause.UNKNOWN, confidence=0.0,
            rationale=f"{specialist.name}: no tools available",
        )

    prompt = _SPECIALIST_PROMPT.format(
        focus=specialist.focus,
        tool_names=", ".join(provider_tools),
        root_causes=", ".join(rc.value for rc in RootCause),
        max_steps=min(settings.investigator_max_steps, 4),
    )
    messages: list[dict[str, str]] = [
        {"role": "system", "content": prompt},
        {"role": "user", "content": f"Incident findings:\n{findings_json}{knowledge_block}"},
    ]
    max_steps = min(settings.investigator_max_steps, 4)
    tokens_used = 0

    for step in range(max_steps):
        remaining = settings.investigator_max_tokens - tokens_used
        if remaining <= 0:
            break
        response = await llm.complete(messages, max_tokens=remaining)
        tokens_used += response.tokens

        payload = _parse_json(response.text)
        if payload is None or payload.get("action") not in ("tools", "diagnose"):
            messages.append({"role": "assistant", "content": response.text})
            messages.append({"role": "user", "content": "Invalid. Use the JSON protocol."})
            continue

        if payload["action"] == "diagnose":
            diag = _to_diagnosis(payload)
            log.info("specialist_diagnosed", specialist=specialist.name,
                     root_cause=diag.root_cause.value, confidence=diag.confidence)
            return diag

        calls = payload.get("calls", []) or []
        results = await asyncio.gather(*(
            _dispatch_tool(str(c.get("tool", "")), dict(c.get("args", {}) or {}),
                           ctx, provider_tools)
            for c in calls
        ))
        messages.append({"role": "assistant", "content": response.text})
        messages.append({"role": "user", "content": "\n".join(
            f"[{c.get('tool', '?')}] {r}" for c, r in zip(calls, results)
        ) or "no results"})

    return Diagnosis(
        root_cause=RootCause.UNKNOWN, confidence=0.0,
        rationale=f"{specialist.name}: budget exhausted",
    )


def synthesize(diagnoses: list[tuple[str, Diagnosis]]) -> Diagnosis:
    """Merge specialist diagnoses: highest-confidence non-UNKNOWN wins."""
    best: Diagnosis | None = None
    all_hypotheses: list[Hypothesis] = []
    all_evidence: list[str] = []

    for name, diag in diagnoses:
        prefixed_evidence = [f"[{name}] {e}" for e in diag.evidence]
        all_evidence.extend(prefixed_evidence)
        for h in diag.hypotheses:
            all_hypotheses.append(h)
        if diag.root_cause is not RootCause.UNKNOWN:
            all_hypotheses.append(Hypothesis(
                root_cause=diag.root_cause,
                confidence=diag.confidence,
                evidence_for=prefixed_evidence,
            ))
            if best is None or diag.confidence > best.confidence:
                best = diag

    if best is None:
        return Diagnosis(
            root_cause=RootCause.UNKNOWN, confidence=0.0,
            rationale="swarm: no specialist identified a root cause",
            evidence=all_evidence,
            hypotheses=all_hypotheses,
        )

    return Diagnosis(
        root_cause=best.root_cause,
        confidence=best.confidence,
        rationale=f"swarm synthesis: {best.rationale}",
        evidence=all_evidence,
        proposed_actions=best.proposed_actions,
        hypotheses=all_hypotheses,
    )


async def swarm_investigate(
    ctx: ToolContext,
    llm: LLMClient,
    findings_json: str,
    knowledge_block: str,
    settings: Settings,
) -> Diagnosis:
    """Fan out specialists in parallel and synthesize."""
    tasks = [
        _run_specialist(s, ctx, llm, findings_json, knowledge_block, settings)
        for s in SPECIALISTS
    ]
    results = await asyncio.gather(*tasks, return_exceptions=True)
    diagnoses: list[tuple[str, Diagnosis]] = []
    for specialist, result in zip(SPECIALISTS, results):
        if isinstance(result, BaseException):
            log.warning("specialist_failed", specialist=specialist.name,
                        error=str(result))
            continue
        diagnoses.append((specialist.name, result))

    merged = synthesize(diagnoses)
    log.info("swarm_synthesized", root_cause=merged.root_cause.value,
             confidence=merged.confidence, specialists=len(diagnoses))
    return merged
