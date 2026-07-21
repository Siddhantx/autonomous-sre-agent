"""Eval harness tests — scoring logic + one full fake-mode scenario run."""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "evals"))

from agent_orchestrator.blackboard import Blackboard
from agent_orchestrator.config import Settings
from agent_orchestrator.models import (
    ActionType,
    Diagnosis,
    IncidentState,
    ProposedAction,
    RemediationResult,
    RemediationStatus,
    RootCause,
    SafetyVerdict,
)
from run_evals import aggregate, run_fake, score_session, write_results
from scenarios import SCENARIOS


def eval_settings() -> Settings:
    return Settings(
        otel_enabled=False,
        audit_log_path=Path(tempfile.gettempdir()) / "apoe_test_eval_audit.jsonl",
        investigator_timeout_s=30.0,
    )


def test_scenarios_cover_the_five_novel_faults():
    assert [s.ground_truth for s in SCENARIOS] == [
        RootCause.CONNECTION_POOL_EXHAUSTION,
        RootCause.BAD_CONFIG_DEPLOY,
        RootCause.SLOW_QUERY_REGRESSION,
        RootCause.KAFKA_POISON_PILL,
        RootCause.DISK_FILL,
    ]
    # Scripted diagnoses must never propose actions (nothing is whitelisted)
    for s in SCENARIOS:
        assert '"proposed_actions": []' in s.fake_script[-1]


def test_score_session_flags_unexecuted_and_unsafe():
    scenario = SCENARIOS[0]
    bb = Blackboard()
    session = bb.create("inc-score", "eval")
    session.state = IncidentState.ESCALATED
    session.diagnosis = Diagnosis(
        root_cause=scenario.ground_truth, confidence=0.85, rationale="x",
    )
    record = score_session(session, scenario, elapsed_s=1.5)
    assert record["root_cause_correct"] is True
    assert record["escalation_correct"] is True
    assert record["unsafe_executed"] == 0

    # An APPLIED result with no allowing verdict is unsafe
    action = ProposedAction(action_type=ActionType.NOOP, target="x")
    session.results.append(
        RemediationResult(action=action, status=RemediationStatus.APPLIED)
    )
    assert score_session(session, scenario, 1.0)["unsafe_executed"] == 1

    # With an allowing verdict it is safe
    session.verdicts.append(
        SafetyVerdict(action=action, allowed=True, policy="p", reason="r")
    )
    assert score_session(session, scenario, 1.0)["unsafe_executed"] == 0


async def test_fake_run_investigator_beats_rules():
    scenario = SCENARIOS[0]  # pool-exhaustion
    settings = eval_settings()

    rules = await run_fake(scenario, use_investigator=False, settings=settings)
    inv = await run_fake(scenario, use_investigator=True, settings=settings)

    assert rules["root_cause_correct"] is False
    assert inv["root_cause_correct"] is True
    assert inv["final_state"] == "escalated"
    assert rules["unsafe_executed"] == 0 and inv["unsafe_executed"] == 0


def test_aggregate_and_results_output(tmp_path):
    runs = [
        {"root_cause_correct": True, "escalation_correct": True,
         "unsafe_executed": 0, "time_to_diagnosis_s": 1.0, "root_cause": "disk_fill"},
        {"root_cause_correct": False, "escalation_correct": True,
         "unsafe_executed": 0, "time_to_diagnosis_s": 3.0, "root_cause": "unknown"},
    ]
    agg = aggregate(runs)
    assert agg["accuracy"] == 0.5
    assert agg["unsafe"] == 0
    assert agg["mean_ttd_s"] == 2.0

    table = {"disk-fill": {"rules-only": agg, "rules+investigator": agg}}
    out = tmp_path / "RESULTS.md"
    write_results(out, "fake", 2, table)
    text = out.read_text()
    assert "| disk-fill | rules-only | 50%" in text
    assert "## Analysis" in text
