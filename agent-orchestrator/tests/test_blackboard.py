"""Unit tests for blackboard state transitions and idempotency tracking."""

from __future__ import annotations

import pytest

from agent_orchestrator.blackboard import (
    Blackboard,
    InvalidTransition,
    UnknownIncident,
    is_legal_transition,
)
from agent_orchestrator.models import (
    ActionType,
    IncidentState,
    ProposedAction,
    RemediationResult,
    RemediationStatus,
)
from tests.conftest import make_finding


def test_happy_path_transitions_are_legal():
    path = [
        IncidentState.DIAGNOSING,
        IncidentState.DIAGNOSED,
        IncidentState.PLANNING,
        IncidentState.REMEDIATING,
        IncidentState.RESOLVED,
    ]
    bb = Blackboard()
    bb.create("inc-1", "test")
    for state in path:
        bb.transition("inc-1", state)
    assert bb.get("inc-1").state is IncidentState.RESOLVED


def test_illegal_transition_raises_and_preserves_state():
    bb = Blackboard()
    bb.create("inc-2", "test")
    with pytest.raises(InvalidTransition):
        bb.transition("inc-2", IncidentState.RESOLVED)  # can't resolve from CREATED
    assert bb.get("inc-2").state is IncidentState.CREATED


def test_terminal_states_have_no_exits():
    for terminal in (
        IncidentState.RESOLVED,
        IncidentState.ESCALATED,
        IncidentState.FAILED,
    ):
        for target in IncidentState:
            assert is_legal_transition(terminal, target) is False


def test_unknown_incident_raises():
    bb = Blackboard()
    with pytest.raises(UnknownIncident):
        bb.get("nope")


def test_duplicate_create_rejected():
    bb = Blackboard()
    bb.create("inc-3", "test")
    with pytest.raises(ValueError):
        bb.create("inc-3", "test")


@pytest.mark.asyncio
async def test_concurrent_finding_appends_are_serialized():
    import asyncio

    bb = Blackboard()
    bb.create("inc-4", "test")
    findings = [make_finding(f"agent-{i}", "sub") for i in range(50)]
    await asyncio.gather(*(bb.add_finding("inc-4", f) for f in findings))
    assert len(bb.get("inc-4").findings) == 50


def test_applied_idempotency_keys_tracks_applied_and_dry_run_only():
    bb = Blackboard()
    bb.create("inc-5", "test")
    action = ProposedAction(
        action_type=ActionType.RESET_CHAOS_SANDBOX, target="chaos-injector"
    )
    bb.add_result(
        "inc-5",
        RemediationResult(action=action, status=RemediationStatus.APPLIED),
    )
    # A failed action must NOT be recorded as applied (so it can be retried).
    failed = ProposedAction(action_type=ActionType.NOOP, target="x")
    bb.add_result(
        "inc-5",
        RemediationResult(action=failed, status=RemediationStatus.FAILED),
    )
    keys = bb.applied_idempotency_keys("inc-5")
    assert action.idempotency_key in keys
    assert failed.idempotency_key not in keys
