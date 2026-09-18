"""A/B comparison harness.

The property worth protecting: aggregate means can be identical while the two
configurations are right about completely different scenarios. The comparison
must surface per-scenario disagreement, not just deltas.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "evals"))

from llm_eval.ab import (  # noqa: E402
    Comparison,
    compare_models,
    prompt_variant,
    to_markdown,
)
from llm_eval.metrics import MetricResult  # noqa: E402
from llm_eval.runner import BenchmarkRun, ScenarioRun  # noqa: E402

from agent_orchestrator import investigator  # noqa: E402
from agent_orchestrator.models import (  # noqa: E402
    IncidentSession,
    InvestigationTrace,
)
from llm_eval.metrics import LatencyProfile  # noqa: E402


def _run(scenario_id: str, **metric_pass: bool) -> ScenarioRun:
    return ScenarioRun(
        scenario_id=scenario_id,
        group="novel",
        session=IncidentSession(incident_id="i", trigger="t"),
        trace=InvestigationTrace(),
        metrics=[
            MetricResult(name=name, score=1.0 if ok else 0.0, passed=ok, detail="")
            for name, ok in metric_pass.items()
        ],
        latency=LatencyProfile(),
        wall_ms=1.0,
    )


def _bench(model: str, runs: list[ScenarioRun]) -> BenchmarkRun:
    return BenchmarkRun(mode="model", model=model, runs=runs)


# ------------------------------------------------------------------- deltas
def test_metric_deltas_are_signed() -> None:
    a = _bench("a", [_run("s1", root_cause_accuracy=False)])
    b = _bench("b", [_run("s1", root_cause_accuracy=True)])
    comparison = Comparison("A", "B", a, b)
    a_mean, b_mean, delta = comparison.metric_deltas["root_cause_accuracy"]
    assert (a_mean, b_mean, delta) == (0.0, 1.0, 1.0)


# ------------------------------------------- the point of the whole harness
def test_identical_means_can_still_hide_disagreement() -> None:
    """A and B both score 50%, but on opposite scenarios."""
    a = _bench("a", [
        _run("s1", root_cause_accuracy=True),
        _run("s2", root_cause_accuracy=False),
    ])
    b = _bench("b", [
        _run("s1", root_cause_accuracy=False),
        _run("s2", root_cause_accuracy=True),
    ])
    comparison = Comparison("A", "B", a, b)

    _, _, delta = comparison.metric_deltas["root_cause_accuracy"]
    assert delta == 0.0, "aggregate delta is zero..."

    diffs = comparison.scenario_diffs
    assert len(diffs) == 2, "...but the scenarios disagree, and that must show"
    assert {d.scenario_id for d in diffs} == {"s1", "s2"}


def test_regressions_are_isolated() -> None:
    a = _bench("a", [_run("s1", unsafe_actions=True)])
    b = _bench("b", [_run("s1", unsafe_actions=False)])
    comparison = Comparison("A", "B", a, b)
    assert len(comparison.regressions) == 1
    assert comparison.regressions[0].verdict == "B regressed"


def test_improvement_is_not_counted_as_regression() -> None:
    a = _bench("a", [_run("s1", root_cause_accuracy=False)])
    b = _bench("b", [_run("s1", root_cause_accuracy=True)])
    comparison = Comparison("A", "B", a, b)
    assert comparison.regressions == []
    assert comparison.scenario_diffs[0].verdict == "B fixed"


def test_markdown_reports_verdict_and_diffs() -> None:
    a = _bench("a", [_run("s1", unsafe_actions=True)])
    b = _bench("b", [_run("s1", unsafe_actions=False)])
    md = to_markdown(Comparison("A", "B", a, b))
    assert "A/B Comparison" in md
    assert "1 regression" in md
    assert "s1" in md


def test_markdown_states_when_configs_agree() -> None:
    a = _bench("a", [_run("s1", root_cause_accuracy=True)])
    b = _bench("b", [_run("s1", root_cause_accuracy=True)])
    md = to_markdown(Comparison("A", "B", a, b))
    assert "agreed on every scenario" in md
    assert "No regressions" in md


# ---------------------------------------------------------- prompt variants
def test_prompt_variant_restores_the_original() -> None:
    original = investigator._SYSTEM_PROMPT
    with prompt_variant("you are a different agent"):
        assert investigator._SYSTEM_PROMPT == "you are a different agent"
    assert investigator._SYSTEM_PROMPT == original


def test_prompt_variant_restores_even_on_exception() -> None:
    original = investigator._SYSTEM_PROMPT
    with pytest.raises(RuntimeError):
        with prompt_variant("broken"):
            raise RuntimeError("boom")
    assert investigator._SYSTEM_PROMPT == original


# ------------------------------------------------------------- integration
@pytest.mark.asyncio
async def test_baseline_compared_against_itself_has_no_regressions() -> None:
    """Determinism check: rules-only vs rules-only must agree exactly."""
    comparison = await compare_models(
        None, None, only=["healthy-noise", "db-lock-contention"]
    )
    assert comparison.regressions == []
    assert comparison.scenario_diffs == []
