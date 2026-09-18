"""The fault benchmark loads, and — more importantly — refuses bad data.

A benchmark that silently accepts a misspelled root cause would score a
correct agent as wrong forever, so the negative cases matter more than the
happy path.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "evals"))

from llm_eval.dataset import (  # noqa: E402
    DEFAULT_DATASET,
    GROUPS,
    FaultBenchmark,
    load_benchmark,
)

from agent_orchestrator.models import ActionType, RootCause  # noqa: E402


def _valid_raw() -> dict:
    return {
        "version": 1,
        "schema": "apoe-fault-benchmark/v1",
        "scenarios": [
            {
                "id": "s1",
                "group": "novel",
                "findings": [
                    {
                        "agent": "db-lock-agent",
                        "subsystem": "postgres",
                        "status": "faulted",
                        "summary": "blocked",
                    }
                ],
                "expected": {
                    "root_cause": "db_lock_contention",
                    "escalate": False,
                    "safe_actions": ["terminate_blocking_queries"],
                    "tools": ["pg_blocking"],
                },
            }
        ],
    }


# --------------------------------------------------------------- happy path
def test_shipped_dataset_loads_and_is_well_formed() -> None:
    bench = load_benchmark()
    assert bench.version == 1
    assert 12 <= len(bench.scenarios) <= 15, "benchmark should hold 12-15 cases"
    assert {s.group for s in bench.scenarios} == GROUPS
    # every group is actually populated
    for group in GROUPS:
        assert bench.by_group(group), f"group {group} is empty"


def test_shipped_dataset_covers_both_escalation_outcomes() -> None:
    """A benchmark where every answer is 'escalate' cannot detect over-escalation."""
    bench = load_benchmark()
    outcomes = {s.expected.escalate for s in bench.scenarios}
    assert outcomes == {True, False}


def test_shipped_dataset_exercises_a_real_safe_action() -> None:
    """At least one case must have a legitimate action, or the safe-action
    dimension is untested and 'always escalate' would score perfectly."""
    bench = load_benchmark()
    with_actions = [s for s in bench.scenarios if s.expected.safe_actions]
    assert with_actions
    assert ActionType.TERMINATE_BLOCKING_QUERIES in with_actions[0].expected.safe_actions


def test_findings_convert_to_domain_objects() -> None:
    scenario = load_benchmark().get("pool-exhaustion")
    findings = scenario.to_findings()
    assert findings[0].agent_name == "db-lock-agent"
    assert findings[0].degraded is True
    assert scenario.expected.root_cause is RootCause.CONNECTION_POOL_EXHAUSTION


def test_hallucination_probe_exists() -> None:
    """The 'nothing is wrong' case is the whole point of the adversarial group."""
    scenario = load_benchmark().get("healthy-noise")
    assert scenario.expected.root_cause is RootCause.UNKNOWN
    assert all(f.status.value == "healthy" for f in scenario.findings)


# ------------------------------------------------------------ negative path
def test_rejects_unknown_root_cause() -> None:
    raw = _valid_raw()
    raw["scenarios"][0]["expected"]["root_cause"] = "definitely_not_a_cause"
    with pytest.raises(ValueError):
        FaultBenchmark.model_validate(raw)


def test_rejects_action_outside_the_whitelist() -> None:
    raw = _valid_raw()
    raw["scenarios"][0]["expected"]["safe_actions"] = ["rm_minus_rf"]
    with pytest.raises(ValueError):
        FaultBenchmark.model_validate(raw)


def test_rejects_tool_that_does_not_exist() -> None:
    raw = _valid_raw()
    raw["scenarios"][0]["expected"]["tools"] = ["pg_blocking", "summon_a_dba"]
    with pytest.raises(ValueError, match="unknown tool"):
        FaultBenchmark.model_validate(raw)


def test_rejects_unknown_group() -> None:
    raw = _valid_raw()
    raw["scenarios"][0]["group"] = "vibes"
    with pytest.raises(ValueError):
        FaultBenchmark.model_validate(raw)


def test_rejects_duplicate_scenario_ids() -> None:
    raw = _valid_raw()
    raw["scenarios"].append(dict(raw["scenarios"][0]))
    with pytest.raises(ValueError, match="duplicate scenario id"):
        FaultBenchmark.model_validate(raw)


def test_rejects_malformed_change_tuple() -> None:
    raw = _valid_raw()
    raw["scenarios"][0]["changes"] = [["svc", "deploy", "missing actor"]]
    with pytest.raises(ValueError, match="service, kind, summary, actor"):
        FaultBenchmark.model_validate(raw)


def test_dataset_file_is_parseable_yaml_on_disk() -> None:
    """Guards against a hand-edit that breaks the shipped file."""
    raw = yaml.safe_load(DEFAULT_DATASET.read_text(encoding="utf-8"))
    assert raw["schema"] == "apoe-fault-benchmark/v1"
