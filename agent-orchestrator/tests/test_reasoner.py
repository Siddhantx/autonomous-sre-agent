"""Unit tests for the reasoner rule engine."""

from __future__ import annotations

from agent_orchestrator.models import ActionType, RootCause, Severity, SubsystemStatus
from agent_orchestrator.reasoner import reason
from tests.conftest import make_finding


def _faulted_db_lock(pid: int = 4242):
    return make_finding(
        "db-lock-agent",
        "postgres",
        status=SubsystemStatus.FAULTED,
        severity=Severity.CRITICAL,
        summary="2 backends blocked",
        metrics={"blocking_backends": 1.0, "primary_blocking_pid": float(pid)},
    )


def test_db_lock_takes_priority_and_proposes_terminate():
    findings = [
        _faulted_db_lock(pid=777),
        make_finding(
            "cpu-agent", "compute", status=SubsystemStatus.FAULTED,
            metrics={"cpu_ratio": 0.99},
        ),
    ]
    dx = reason(findings)
    assert dx.root_cause is RootCause.DB_LOCK_CONTENTION  # priority over cpu
    assert dx.confidence >= 0.9
    assert len(dx.proposed_actions) == 1
    action = dx.proposed_actions[0]
    assert action.action_type is ActionType.TERMINATE_BLOCKING_QUERIES
    assert action.params["pid"] == 777


def test_cpu_saturation_proposes_reset():
    findings = [
        make_finding(
            "cpu-agent", "compute", status=SubsystemStatus.FAULTED,
            metrics={"cpu_ratio": 0.97},
        )
    ]
    dx = reason(findings)
    assert dx.root_cause is RootCause.CPU_SATURATION
    assert dx.proposed_actions[0].action_type is ActionType.RESET_CHAOS_SANDBOX


def test_memory_leak_proposes_reset():
    findings = [
        make_finding(
            "memory-agent", "memory", status=SubsystemStatus.FAULTED,
            metrics={"resident_bytes": 900 * 1024 * 1024},
        )
    ]
    dx = reason(findings)
    assert dx.root_cause is RootCause.MEMORY_LEAK
    assert dx.proposed_actions[0].action_type is ActionType.RESET_CHAOS_SANDBOX


def test_cache_degraded_has_no_auto_action():
    findings = [
        make_finding("cache-agent", "redis", status=SubsystemStatus.FAULTED)
    ]
    dx = reason(findings)
    assert dx.root_cause is RootCause.CACHE_UNAVAILABLE
    assert dx.proposed_actions == []


def test_all_healthy_is_unknown_with_no_actions():
    findings = [
        make_finding("db-lock-agent", "postgres"),
        make_finding("cpu-agent", "compute"),
    ]
    dx = reason(findings)
    assert dx.root_cause is RootCause.UNKNOWN
    assert dx.confidence == 0.0
    assert dx.proposed_actions == []


def test_degraded_findings_do_not_trigger_actions():
    # A degraded (unavailable) signal must never be read as a fault.
    findings = [
        make_finding(
            "cpu-agent", "compute", status=SubsystemStatus.UNAVAILABLE,
            degraded=True,
        )
    ]
    dx = reason(findings)
    assert dx.root_cause is RootCause.UNKNOWN


def test_reasoner_is_deterministic():
    findings = [_faulted_db_lock(pid=1)]
    assert reason(findings) == reason(findings)
