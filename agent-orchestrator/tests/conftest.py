"""Shared test fixtures / factories.

Tests here exercise the *pure* critical modules (reasoner, safety compiler,
blackboard) with no external I/O, so no live Postgres/Redis/Kafka is required.
"""

from __future__ import annotations

from agent_orchestrator.models import (
    Finding,
    Severity,
    SubsystemStatus,
)


def make_finding(
    agent_name: str,
    subsystem: str,
    status: SubsystemStatus = SubsystemStatus.HEALTHY,
    severity: Severity = Severity.INFO,
    summary: str = "",
    metrics: dict[str, float] | None = None,
    degraded: bool = False,
) -> Finding:
    return Finding(
        agent_name=agent_name,
        subsystem=subsystem,
        status=status,
        severity=severity,
        summary=summary or f"{subsystem} finding",
        metrics=metrics or {},
        degraded=degraded,
    )
