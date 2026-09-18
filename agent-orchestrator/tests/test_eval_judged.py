"""Judged-tier mapping: APOE traces -> DeepEval test cases.

The mapping is pure and testable without DeepEval installed, which is the
point — the deterministic tier that gates CI must never import it. These run
in the main environment, where DeepEval is deliberately absent, so they also
pin the "not installed" behaviour.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "evals"))

from llm_eval.judged import (  # noqa: E402
    REASONING_RUBRIC,
    build_case,
    deepeval_available,
    diagnosis_as_text,
    require_deepeval,
    trace_as_text,
)

from agent_orchestrator.models import (  # noqa: E402
    Diagnosis,
    Hypothesis,
    InvestigationTrace,
    RootCause,
    ToolCallRecord,
    TraceStep,
)


def _trace() -> InvestigationTrace:
    return InvestigationTrace(
        context="Incident findings: postgres unavailable",
        steps=[
            TraceStep(
                step=0, action="tools", llm_latency_ms=10.0, tokens=5,
                tool_calls=[
                    ToolCallRecord(tool="pg_stat_activity", args={"limit": 5},
                                   result='{"rows": 100}', ok=True),
                    ToolCallRecord(tool="redis_info", args={},
                                   result='{"error": "refused"}', ok=False),
                ],
            ),
            TraceStep(step=1, action="diagnose", llm_latency_ms=8.0, tokens=7,
                      raw_response='{"root_cause":"connection_pool_exhaustion"}'),
        ],
    )


# ------------------------------------------------------------ optionality
def test_deepeval_is_absent_from_the_main_environment() -> None:
    """The CI-gating tier must not depend on the judged tier."""
    assert deepeval_available() is False


def test_missing_deepeval_gives_actionable_instructions() -> None:
    with pytest.raises(ImportError) as exc:
        require_deepeval()
    message = str(exc.value)
    assert "requirements-eval.txt" in message
    assert "opentelemetry-sdk" in message  # explains *why* it is separate
    assert "deterministic metrics" in message


# ---------------------------------------------------------------- mapping
def test_diagnosis_text_carries_cause_confidence_and_evidence() -> None:
    diagnosis = Diagnosis(
        root_cause=RootCause.DISK_FILL, confidence=0.82,
        rationale="volume at 98%", evidence=["df shows 98%"],
        hypotheses=[Hypothesis(root_cause=RootCause.CPU_SATURATION, confidence=0.1)],
    )
    text = diagnosis_as_text(diagnosis)
    assert "disk_fill" in text
    assert "0.82" in text
    assert "volume at 98%" in text
    assert "df shows 98%" in text
    assert "cpu_saturation" in text  # the differential is part of the reasoning


def test_missing_diagnosis_is_stated_not_faked() -> None:
    assert "No diagnosis" in diagnosis_as_text(None)


def test_trace_text_includes_tool_calls_and_failures() -> None:
    text = trace_as_text(_trace())
    assert "pg_stat_activity" in text
    assert "{'limit': 5}" in text
    assert "ERROR" in text, "a failed tool call must be visible to the judge"
    assert "redis_info" in text


def test_empty_trace_is_stated_explicitly() -> None:
    assert "No reasoning steps" in trace_as_text(InvestigationTrace())


def test_case_grounds_on_what_the_agent_actually_saw() -> None:
    """Faithfulness is scored against gathered evidence, not ground truth."""
    diagnosis = Diagnosis(root_cause=RootCause.CONNECTION_POOL_EXHAUSTION,
                          confidence=0.9, rationale="pool full")
    case = build_case("postgres is down", diagnosis, _trace())
    assert case.input == "postgres is down"
    assert "connection_pool_exhaustion" in case.actual_output
    # seed context + both tool results
    joined = "\n".join(case.retrieval_context)
    assert "postgres unavailable" in joined
    assert '{"rows": 100}' in joined
    assert "pg_stat_activity" in case.reasoning_trace


def test_case_with_no_evidence_does_not_present_an_empty_context() -> None:
    case = build_case("something broke", None, InvestigationTrace())
    assert case.retrieval_context == ["No evidence was gathered."]


# ----------------------------------------------------------------- rubric
def test_reasoning_rubric_rewards_honest_uncertainty() -> None:
    """The rubric must not punish the agent for correctly saying 'I can't tell'
    — otherwise it trains exactly the overconfidence we test for elsewhere."""
    assert "cannot determine" in REASONING_RUBRIC
    assert "GOOD answer" in REASONING_RUBRIC
    assert "score highly" in REASONING_RUBRIC
