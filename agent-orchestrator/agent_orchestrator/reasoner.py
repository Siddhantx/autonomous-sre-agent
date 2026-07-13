"""The reasoner: a deterministic, pure rule engine.

Input is the set of :class:`Finding`s on the blackboard; output is a single
:class:`Diagnosis` (root cause + confidence + proposed remediations). It does
**no I/O** — it is a pure function of its inputs, which is exactly why it is the
most heavily unit-tested module.

Rules are evaluated in priority order; the first rule that matches wins. This
keeps the engine explainable ("we did X because rule R fired on evidence E") —
a hard requirement when a human SRE has to audit an autonomous action.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from .models import (
    ActionType,
    Diagnosis,
    Finding,
    ProposedAction,
    RootCause,
    SubsystemStatus,
)


def _by_agent(findings: list[Finding]) -> dict[str, Finding]:
    return {f.agent_name: f for f in findings}


@dataclass(frozen=True)
class Rule:
    """A single named diagnostic rule."""

    name: str
    matcher: Callable[[dict[str, Finding]], Diagnosis | None]


def _faulted(finding: Finding | None) -> bool:
    return finding is not None and finding.status is SubsystemStatus.FAULTED


# --- Rule implementations --------------------------------------------------
def _rule_db_lock(f: dict[str, Finding]) -> Diagnosis | None:
    finding = f.get("db-lock-agent")
    if not _faulted(finding):
        return None
    assert finding is not None
    pid = int(finding.metrics.get("primary_blocking_pid", 0))
    return Diagnosis(
        root_cause=RootCause.DB_LOCK_CONTENTION,
        confidence=0.95,
        rationale=(
            "One or more Postgres backends are blocking others "
            "(pg_blocking_pids > 0); the lead blocker must be terminated to "
            "release the lock."
        ),
        evidence=[finding.summary],
        proposed_actions=[
            ProposedAction(
                action_type=ActionType.TERMINATE_BLOCKING_QUERIES,
                target="postgres",
                params={"pid": pid},
                rationale=f"terminate lead blocking backend pid={pid}",
            )
        ],
    )


def _rule_cpu(f: dict[str, Finding]) -> Diagnosis | None:
    finding = f.get("cpu-agent")
    if not _faulted(finding):
        return None
    assert finding is not None
    return Diagnosis(
        root_cause=RootCause.CPU_SATURATION,
        confidence=0.70,
        rationale="Sustained CPU saturation detected across the service fleet.",
        evidence=[finding.summary],
        proposed_actions=[
            ProposedAction(
                action_type=ActionType.RESET_CHAOS_SANDBOX,
                target="chaos-injector",
                rationale="clear the runaway compute load in the lab sandbox",
            )
        ],
    )


def _rule_memory(f: dict[str, Finding]) -> Diagnosis | None:
    finding = f.get("memory-agent")
    if not _faulted(finding):
        return None
    assert finding is not None
    return Diagnosis(
        root_cause=RootCause.MEMORY_LEAK,
        confidence=0.70,
        rationale="Resident memory growth consistent with a heap leak (OOM risk).",
        evidence=[finding.summary],
        proposed_actions=[
            ProposedAction(
                action_type=ActionType.RESET_CHAOS_SANDBOX,
                target="chaos-injector",
                rationale="release leaked allocations in the lab sandbox",
            )
        ],
    )


def _rule_cache(f: dict[str, Finding]) -> Diagnosis | None:
    finding = f.get("cache-agent")
    if not _faulted(finding):
        return None
    assert finding is not None
    # Latency is not fixed by flushing; propose no automatic action and let
    # safety/orchestrator escalate to a human.
    return Diagnosis(
        root_cause=RootCause.CACHE_UNAVAILABLE,
        confidence=0.60,
        rationale="Redis latency degraded beyond threshold; no safe autoremediation.",
        evidence=[finding.summary],
        proposed_actions=[],
    )


def _rule_kafka(f: dict[str, Finding]) -> Diagnosis | None:
    finding = f.get("kafka-lag-agent")
    if not _faulted(finding):
        return None
    assert finding is not None
    return Diagnosis(
        root_cause=RootCause.KAFKA_CONSUMER_LAG,
        confidence=0.60,
        rationale="Payment consumer lag exceeds threshold; requires capacity review.",
        evidence=[finding.summary],
        proposed_actions=[],
    )


# Priority order: most-actionable / highest-confidence first.
DEFAULT_RULES: tuple[Rule, ...] = (
    Rule("db-lock-contention", _rule_db_lock),
    Rule("cpu-saturation", _rule_cpu),
    Rule("memory-leak", _rule_memory),
    Rule("cache-degraded", _rule_cache),
    Rule("kafka-lag", _rule_kafka),
)


def reason(
    findings: list[Finding], rules: tuple[Rule, ...] = DEFAULT_RULES
) -> Diagnosis:
    """Return the diagnosis from the first matching rule, else UNKNOWN."""
    indexed = _by_agent(findings)
    for rule in rules:
        diagnosis = rule.matcher(indexed)
        if diagnosis is not None:
            return diagnosis
    faulted = [f.summary for f in findings if f.is_fault]
    return Diagnosis(
        root_cause=RootCause.UNKNOWN,
        confidence=0.0,
        rationale="No rule matched the observed findings.",
        evidence=faulted,
        proposed_actions=[],
    )
