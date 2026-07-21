"""Tests for multi-agent swarm investigation."""

from __future__ import annotations

import json

from agent_orchestrator.models import Diagnosis, Hypothesis, RootCause
from agent_orchestrator.swarm import synthesize


def _diag(root: RootCause, confidence: float, evidence: list[str] | None = None,
          hypotheses: list[Hypothesis] | None = None) -> Diagnosis:
    return Diagnosis(
        root_cause=root, confidence=confidence, rationale=f"test {root.value}",
        evidence=evidence or [], hypotheses=hypotheses or [],
    )


def test_synthesize_picks_highest_confidence():
    diagnoses = [
        ("db", _diag(RootCause.SLOW_QUERY_REGRESSION, 0.6, ["slow queries"])),
        ("infra", _diag(RootCause.DISK_FILL, 0.85, ["disk at 96%"])),
        ("app", _diag(RootCause.UNKNOWN, 0.0)),
    ]
    merged = synthesize(diagnoses)
    assert merged.root_cause is RootCause.DISK_FILL
    assert merged.confidence == 0.85
    assert any("disk at 96%" in e for e in merged.evidence)
    assert any("slow queries" in e for e in merged.evidence)


def test_synthesize_all_unknown_returns_unknown():
    diagnoses = [
        ("db", _diag(RootCause.UNKNOWN, 0.0)),
        ("infra", _diag(RootCause.UNKNOWN, 0.0)),
    ]
    merged = synthesize(diagnoses)
    assert merged.root_cause is RootCause.UNKNOWN
    assert merged.confidence == 0.0


def test_synthesize_collects_hypotheses():
    h1 = Hypothesis(root_cause=RootCause.DISK_FILL, confidence=0.8,
                     evidence_for=["full disk"])
    h2 = Hypothesis(root_cause=RootCause.SLOW_QUERY_REGRESSION, confidence=0.3,
                     evidence_against=["no seq scans"])
    diagnoses = [
        ("db", _diag(RootCause.DISK_FILL, 0.8, hypotheses=[h1, h2])),
        ("app", _diag(RootCause.UNKNOWN, 0.0)),
    ]
    merged = synthesize(diagnoses)
    assert len(merged.hypotheses) >= 2


def test_synthesize_empty_input():
    merged = synthesize([])
    assert merged.root_cause is RootCause.UNKNOWN


async def test_swarm_with_scripted_llm():
    """Full swarm run with a scripted LLM — verifies parallel fan-out."""
    from types import SimpleNamespace
    from agent_orchestrator.config import Settings
    from agent_orchestrator.investigator import LLMResponse, ToolContext
    from agent_orchestrator.models import IncidentSession, IncidentState
    from agent_orchestrator.blackboard import Blackboard
    from agent_orchestrator.swarm import swarm_investigate

    class AllUnknownLLM:
        async def complete(self, messages, max_tokens):
            return LLMResponse(
                text=json.dumps({
                    "action": "diagnose", "root_cause": "unknown",
                    "confidence": 0.0, "rationale": "nothing found",
                }),
                tokens=10,
            )

    settings = Settings(otel_enabled=False, swarm_enabled=True)
    session = IncidentSession(incident_id="inc-sw1", trigger="test",
                              state=IncidentState.DIAGNOSING)
    bb = Blackboard()
    bb.create("inc-sw1", "test")
    connectors = SimpleNamespace(
        postgres=SimpleNamespace(
            fetch=lambda *a: [],
            blocking_backends=lambda: [],
        ),
        redis=SimpleNamespace(info=lambda: {}, slowlog=lambda n: [],
                              key_sample=lambda n: []),
        kafka=SimpleNamespace(total_consumer_lag=lambda g, t: 0,
                              topic_offsets=lambda t: {}),
        prometheus=SimpleNamespace(instant_query=lambda q: None,
                                   range_query=lambda q, s, e, st: []),
        loki=SimpleNamespace(search=lambda *a, **kw: []),
    )
    ctx = ToolContext(connectors, session, bb, settings)  # type: ignore[arg-type]
    result = await swarm_investigate(ctx, AllUnknownLLM(), "[]", "", settings)
    assert result.root_cause is RootCause.UNKNOWN
