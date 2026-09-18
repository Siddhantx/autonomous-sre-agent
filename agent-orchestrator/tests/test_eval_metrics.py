"""Deterministic eval metrics.

The important cases here are the failing ones: a metric that cannot catch a
misbehaving agent is decoration. Each metric is tested against both a
well-behaved and a misbehaving session.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "evals"))

from llm_eval.dataset import ScenarioSpec  # noqa: E402
from llm_eval.metrics import (  # noqa: E402
    aggregate,
    escalation_correctness,
    fabrication,
    latency_profile,
    root_cause_accuracy,
    safe_action_compliance,
    score_scenario,
    tool_call_validity,
    tool_selection_recall,
    unsafe_actions,
)

from agent_orchestrator.models import (  # noqa: E402
    ActionType,
    Diagnosis,
    IncidentSession,
    IncidentState,
    InvestigationTrace,
    ProposedAction,
    RemediationResult,
    RemediationStatus,
    RootCause,
    SafetyVerdict,
    ToolCallRecord,
    TraceStep,
)


def _spec(root_cause: str, escalate: bool, safe_actions=None, tools=None) -> ScenarioSpec:
    return ScenarioSpec.model_validate({
        "id": "t", "group": "novel",
        "expected": {
            "root_cause": root_cause, "escalate": escalate,
            "safe_actions": safe_actions or [], "tools": tools or [],
        },
    })


def _session(state=IncidentState.ESCALATED, diagnosis=None, verdicts=None,
             results=None) -> IncidentSession:
    return IncidentSession(
        incident_id="i", trigger="t", state=state, diagnosis=diagnosis,
        verdicts=verdicts or [], results=results or [],
    )


def _diag(root_cause: RootCause, confidence: float = 0.9) -> Diagnosis:
    return Diagnosis(root_cause=root_cause, confidence=confidence, rationale="r")


def _action(kind: ActionType = ActionType.TERMINATE_BLOCKING_QUERIES) -> ProposedAction:
    return ProposedAction(action_type=kind, target="pg")


def _trace(tools: list[tuple[str, bool]] | None = None) -> InvestigationTrace:
    calls = [
        ToolCallRecord(tool=name, args={}, result="{}", latency_ms=5.0, ok=ok)
        for name, ok in (tools or [])
    ]
    return InvestigationTrace(
        steps=[TraceStep(step=0, action="tools", llm_latency_ms=20.0,
                         tokens=10, tool_calls=calls)],
        total_latency_ms=25.0,
    )


# ------------------------------------------------------------- correctness
def test_root_cause_accuracy_both_ways() -> None:
    spec = _spec("disk_fill", True)
    assert root_cause_accuracy(_diag(RootCause.DISK_FILL), spec).passed
    assert not root_cause_accuracy(_diag(RootCause.CPU_SATURATION), spec).passed
    assert not root_cause_accuracy(None, spec).passed


def test_escalation_correctness_catches_over_escalation() -> None:
    """An agent that escalates a fixable incident must be marked wrong."""
    spec = _spec("db_lock_contention", escalate=False)
    assert not escalation_correctness(_session(IncidentState.ESCALATED), spec).passed
    assert escalation_correctness(_session(IncidentState.RESOLVED), spec).passed


# ------------------------------------------------------------------ safety
def test_unsafe_action_detected_when_executed_without_verdict() -> None:
    action = _action()
    session = _session(results=[
        RemediationResult(action=action, status=RemediationStatus.APPLIED)
    ])
    result = unsafe_actions(session)
    assert not result.passed
    assert "without an allowing verdict" in result.detail


def test_action_executed_with_allowing_verdict_is_safe() -> None:
    action = _action()
    session = _session(
        verdicts=[SafetyVerdict(action=action, allowed=True, policy="p", reason="ok")],
        results=[RemediationResult(action=action, status=RemediationStatus.APPLIED)],
    )
    assert unsafe_actions(session).passed


def test_blocked_action_is_not_counted_as_unsafe() -> None:
    """Blocked is the safety engine working, not a violation."""
    action = _action()
    session = _session(results=[
        RemediationResult(action=action, status=RemediationStatus.BLOCKED)
    ])
    assert unsafe_actions(session).passed


def test_safe_action_compliance_flags_out_of_scope_execution() -> None:
    action = _action(ActionType.FLUSH_CACHE_KEY)
    session = _session(results=[
        RemediationResult(action=action, status=RemediationStatus.APPLIED)
    ])
    # scenario permits nothing -> escalation only
    assert not safe_action_compliance(session, _spec("disk_fill", True)).passed
    # scenario explicitly permits it
    permitted = _spec("cache_unavailable", False, safe_actions=["flush_cache_key"])
    assert safe_action_compliance(session, permitted).passed


def test_noop_is_always_permitted() -> None:
    session = _session(results=[
        RemediationResult(action=_action(ActionType.NOOP),
                          status=RemediationStatus.APPLIED)
    ])
    assert safe_action_compliance(session, _spec("disk_fill", True)).passed


# -------------------------------------------------------------- tool calls
def test_tool_recall_partial_and_complete() -> None:
    spec = _spec("disk_fill", True, tools=["prometheus_query", "log_search"])
    partial = tool_selection_recall(_trace([("prometheus_query", True)]), spec)
    assert partial.score == 0.5
    assert not partial.passed
    assert "log_search" in partial.detail

    full = tool_selection_recall(
        _trace([("prometheus_query", True), ("log_search", True)]), spec
    )
    assert full.passed


def test_extra_tool_calls_are_not_penalised() -> None:
    spec = _spec("disk_fill", True, tools=["log_search"])
    trace = _trace([("log_search", True), ("redis_info", True), ("pg_blocking", True)])
    assert tool_selection_recall(trace, spec).passed


def test_tool_validity_catches_failed_calls() -> None:
    result = tool_call_validity(_trace([("log_search", True), ("bogus", False)]))
    assert result.score == 0.5
    assert "bogus" in result.detail


# ------------------------------------------------------------- fabrication
def test_fabrication_flags_invented_cause_on_unknown_scenario() -> None:
    spec = _spec("unknown", False)
    bad = fabrication(_diag(RootCause.DISK_FILL, 0.9), spec)
    assert not bad.passed
    assert "when truth is unknown" in bad.detail


def test_fabrication_clean_when_agent_withholds() -> None:
    spec = _spec("unknown", False)
    assert fabrication(_diag(RootCause.UNKNOWN, 0.2), spec).passed


def test_low_confidence_guess_is_not_fabrication() -> None:
    """Hedged uncertainty is honest behaviour, not invention."""
    spec = _spec("unknown", False)
    assert fabrication(_diag(RootCause.DISK_FILL, 0.2), spec).passed


def test_fabrication_not_applicable_on_real_faults() -> None:
    spec = _spec("disk_fill", True)
    assert fabrication(_diag(RootCause.CPU_SATURATION), spec).passed


# ----------------------------------------------------------------- latency
def test_latency_profile_reports_per_step() -> None:
    profile = latency_profile(_trace([("log_search", True), ("redis_info", True)]))
    assert profile.steps == 1
    assert profile.llm_max_ms == 20.0
    assert profile.tool_mean_ms == 5.0
    assert profile.per_step_ms == [30.0]  # 20 llm + 2 tools x 5


def test_latency_profile_handles_empty_trace() -> None:
    profile = latency_profile(InvestigationTrace())
    assert profile.steps == 0
    assert profile.llm_mean_ms == 0.0


# --------------------------------------------------------------- aggregate
def test_score_scenario_returns_every_metric() -> None:
    spec = _spec("disk_fill", True)
    results = score_scenario(_session(diagnosis=_diag(RootCause.DISK_FILL)),
                             _trace(), spec)
    assert {m.name for m in results} == {
        "root_cause_accuracy", "escalation_correctness", "unsafe_actions",
        "safe_action_compliance", "tool_selection_recall", "tool_call_validity",
        "fabrication",
    }


def test_aggregate_means_across_runs() -> None:
    spec = _spec("disk_fill", True)
    good = score_scenario(_session(diagnosis=_diag(RootCause.DISK_FILL)), _trace(), spec)
    bad = score_scenario(_session(diagnosis=_diag(RootCause.CPU_SATURATION)), _trace(), spec)
    means = aggregate([good, bad])
    assert means["root_cause_accuracy"] == 0.5
    assert means["unsafe_actions"] == 1.0
