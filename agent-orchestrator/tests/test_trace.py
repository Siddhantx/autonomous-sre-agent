"""Investigation trace capture — the substrate the eval metrics run on.

These assert the trace records exactly what the DeepEval metrics need:
tool names + arguments (tool-call correctness), the raw model text
(reasoning quality), per-step latency, and the grounding context
(faithfulness / hallucination).
"""

from __future__ import annotations

import json

import pytest

from agent_orchestrator.blackboard import Blackboard
from agent_orchestrator.config import Settings
from agent_orchestrator.investigator import LLMResponse, investigate
from agent_orchestrator.models import (
    Finding,
    IncidentSession,
    InvestigationTrace,
    Severity,
    SubsystemStatus,
)


class ScriptedLLM:
    """Replays a fixed transcript; the last reply repeats if overrun."""

    def __init__(self, script: list[str]) -> None:
        self._script = list(script)

    async def complete(self, messages, max_tokens) -> LLMResponse:
        text = self._script.pop(0) if len(self._script) > 1 else self._script[0]
        return LLMResponse(text=text, tokens=42)


def _session() -> IncidentSession:
    return IncidentSession(
        incident_id="inc-trace-1",
        trigger="test",
        findings=[
            Finding(
                agent_name="db-agent",
                subsystem="postgres",
                status=SubsystemStatus.FAULTED,
                severity=Severity.CRITICAL,
                summary="connection pool saturated",
                metrics={"active_connections": 100.0},
            )
        ],
    )


def _settings() -> Settings:
    return Settings(otel_enabled=False, investigator_max_steps=4)


class _Conn:
    """Minimal connector stub; the scripted tool call returns an error payload,
    which is itself worth asserting (ok=False must be recorded, not crash)."""


@pytest.mark.asyncio
async def test_trace_captures_tools_args_and_diagnosis() -> None:
    script = [
        json.dumps({
            "action": "tools",
            "calls": [{"tool": "pg_stat_activity", "args": {"limit": 5}}],
        }),
        json.dumps({
            "action": "diagnose",
            "root_cause": "connection_pool_exhaustion",
            "confidence": 0.9,
            "rationale": "pool saturated",
            "evidence": ["active_connections=100"],
            "actions": [],
        }),
    ]
    trace = InvestigationTrace()
    diagnosis = await investigate(
        _session(), Blackboard(), _Conn(), _settings(),  # type: ignore[arg-type]
        ScriptedLLM(script), None, trace,
    )

    assert diagnosis.root_cause.value == "connection_pool_exhaustion"
    assert trace.outcome == "diagnosed"
    assert [s.action for s in trace.steps] == ["tools", "diagnose"]

    # tool-call correctness needs name AND args
    assert trace.tools_called == ["pg_stat_activity"]
    assert trace.steps[0].tool_calls[0].args == {"limit": 5}

    # reasoning quality needs the raw model text
    assert "connection_pool_exhaustion" in trace.steps[-1].raw_response

    # latency per step, and totals
    assert trace.steps[0].tool_calls[0].latency_ms >= 0.0
    assert trace.total_tokens == 84  # 42 per call, two calls
    assert trace.total_latency_ms > 0.0

    # grounding context for faithfulness / hallucination
    assert "connection pool saturated" in trace.context
    assert len(trace.retrieval_context) >= 2  # seed + one tool result


@pytest.mark.asyncio
async def test_trace_records_failed_tool_without_crashing() -> None:
    script = [
        json.dumps({
            "action": "tools",
            "calls": [{"tool": "no_such_tool", "args": {}}],
        }),
        json.dumps({
            "action": "diagnose", "root_cause": "unknown", "confidence": 0.1,
            "rationale": "n/a", "evidence": [], "actions": [],
        }),
    ]
    trace = InvestigationTrace()
    await investigate(
        _session(), Blackboard(), _Conn(), _settings(),  # type: ignore[arg-type]
        ScriptedLLM(script), None, trace,
    )
    call = trace.steps[0].tool_calls[0]
    assert call.tool == "no_such_tool"
    assert call.ok is False


@pytest.mark.asyncio
async def test_invalid_reply_is_traced_and_budget_exhaustion_recorded() -> None:
    trace = InvestigationTrace()
    diagnosis = await investigate(
        _session(), Blackboard(), _Conn(), _settings(),  # type: ignore[arg-type]
        ScriptedLLM(["not json at all"]), None, trace,
    )
    assert diagnosis.root_cause.value == "unknown"
    assert trace.outcome == "budget_exhausted"
    assert {s.action for s in trace.steps} == {"invalid"}
    assert len(trace.steps) == 4  # investigator_max_steps


@pytest.mark.asyncio
async def test_trace_is_optional_and_costs_nothing() -> None:
    """Production path: no trace passed, behaviour unchanged."""
    script = [json.dumps({
        "action": "diagnose", "root_cause": "disk_fill", "confidence": 0.8,
        "rationale": "disk", "evidence": [], "actions": [],
    })]
    diagnosis = await investigate(
        _session(), Blackboard(), _Conn(), _settings(),  # type: ignore[arg-type]
        ScriptedLLM(script), None,
    )
    assert diagnosis.root_cause.value == "disk_fill"
