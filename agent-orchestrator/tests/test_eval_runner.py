"""Benchmark runner: executes scenarios through the real pipeline.

The runner deliberately goes through Orchestrator rather than calling
investigate() directly, so the safety metrics observe genuine policy verdicts.
These tests assert that property holds.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "evals"))

from llm_eval.dataset import load_benchmark  # noqa: E402
from llm_eval.runner import (  # noqa: E402
    RecordedMutations,
    build_settings,
    connectors_for,
    run_benchmark,
    run_scenario,
)


@pytest.mark.asyncio
async def test_rules_only_baseline_runs_every_scenario() -> None:
    result = await run_benchmark()
    assert result.mode == "rules-only"
    assert len(result.runs) == len(load_benchmark().scenarios)
    # Every run produced a full metric set.
    assert all(len(r.metrics) == 7 for r in result.runs)


@pytest.mark.asyncio
async def test_safety_invariant_holds_across_the_whole_benchmark() -> None:
    """The load-bearing assertion: no action ever executes without a verdict."""
    result = await run_benchmark()
    for run in result.runs:
        unsafe = next(m for m in run.metrics if m.name == "unsafe_actions")
        assert unsafe.passed, f"{run.scenario_id}: {unsafe.detail}"


@pytest.mark.asyncio
async def test_safe_action_path_actually_executes() -> None:
    """Regression guard: the fake connectors were read-only, so the one
    scenario whose correct outcome is 'take the safe action' could never
    pass. It must now resolve AND record the mutation."""
    spec = load_benchmark().get("db-lock-contention")
    run = await run_scenario(spec, build_settings(None, ""))
    assert run.session.state.value == "resolved"
    assert [c[0] for c in run.executed] == ["terminate_backend"]
    compliance = next(m for m in run.metrics if m.name == "safe_action_compliance")
    assert compliance.passed


@pytest.mark.asyncio
async def test_over_reach_is_caught_as_non_compliance() -> None:
    """two-faults-at-once permits no actions; acting there must be flagged."""
    spec = load_benchmark().get("two-faults-at-once")
    run = await run_scenario(spec, build_settings(None, ""))
    if run.executed:  # baseline rules do act here
        compliance = next(
            m for m in run.metrics if m.name == "safe_action_compliance"
        )
        assert not compliance.passed


@pytest.mark.asyncio
async def test_hallucination_probe_is_clean_for_rules_baseline() -> None:
    """Rules invent nothing, so the fabrication metric must be clean."""
    spec = load_benchmark().get("healthy-noise")
    run = await run_scenario(spec, build_settings(None, ""))
    assert run.passed


def test_connectors_expose_read_and_mutate_surfaces() -> None:
    spec = load_benchmark().get("db-lock-contention")
    connectors, mutations = connectors_for(spec)
    assert isinstance(mutations, RecordedMutations)
    for attr in ("postgres", "redis", "chaos"):
        assert hasattr(connectors, attr)
    assert hasattr(connectors.postgres, "terminate_backend")  # mutating
    assert hasattr(connectors.postgres, "fetch")              # read-only


def test_rules_only_settings_disable_the_investigator() -> None:
    """Empty llm_model is what turns the investigator off — assert it."""
    assert build_settings(None, "").llm_model == ""
    assert build_settings("qwen2.5:3b", "http://x/v1").llm_model == "qwen2.5:3b"
