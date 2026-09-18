"""A/B harness: run the same benchmark under two configurations and compare.

Two axes are supported.

**Models** are the natural axis and need no tricks — ``run_benchmark`` already
takes a model, so an A/B is two calls and a diff.

**Prompt versions** need a seam that production does not otherwise have. The
system prompt is a module constant, so ``prompt_variant`` swaps it for the
duration of a run and restores it afterwards. That is a deliberate, contained
piece of test scaffolding: it lives in the eval layer, never in the agent, and
it is the only place in this package that reaches into module state.

The comparison deliberately reports *per-scenario disagreements* as well as
aggregate deltas. Two configurations can post identical means while being
right about completely different scenarios, and an aggregate-only view hides
exactly the regression you most want to catch.
"""

from __future__ import annotations

import sys
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator

_EVALS = Path(__file__).resolve().parents[1]
if str(_EVALS) not in sys.path:
    sys.path.insert(0, str(_EVALS))

from agent_orchestrator import investigator  # noqa: E402

from .metrics import aggregate  # noqa: E402
from .report import COLUMNS  # noqa: E402
from .runner import BenchmarkRun, run_benchmark  # noqa: E402


@contextmanager
def prompt_variant(system_prompt: str) -> Iterator[None]:
    """Temporarily replace the investigator's system prompt.

    Test scaffolding, not a feature: it mutates a module constant and restores
    it on exit, including on exception. Not safe under concurrent runs in the
    same process — A/B passes are sequential for exactly this reason.
    """
    original = investigator._SYSTEM_PROMPT
    investigator._SYSTEM_PROMPT = system_prompt
    try:
        yield
    finally:
        investigator._SYSTEM_PROMPT = original


@dataclass(frozen=True)
class ScenarioDiff:
    """One scenario where the two configurations disagreed."""

    scenario_id: str
    metric: str
    a_passed: bool
    b_passed: bool

    @property
    def verdict(self) -> str:
        return "B fixed" if self.b_passed else "B regressed"


@dataclass(frozen=True)
class Comparison:
    label_a: str
    label_b: str
    a: BenchmarkRun
    b: BenchmarkRun

    @property
    def metric_deltas(self) -> dict[str, tuple[float, float, float]]:
        """metric -> (a_mean, b_mean, delta)."""
        a_means = aggregate([r.metrics for r in self.a.runs])
        b_means = aggregate([r.metrics for r in self.b.runs])
        return {
            name: (a_means.get(name, 0.0), b_means.get(name, 0.0),
                   b_means.get(name, 0.0) - a_means.get(name, 0.0))
            for name in sorted(set(a_means) | set(b_means))
        }

    @property
    def scenario_diffs(self) -> list[ScenarioDiff]:
        """Per-scenario, per-metric disagreements between the two runs."""
        b_by_id = {r.scenario_id: r for r in self.b.runs}
        diffs: list[ScenarioDiff] = []
        for run_a in self.a.runs:
            run_b = b_by_id.get(run_a.scenario_id)
            if run_b is None:
                continue
            b_metrics = {m.name: m for m in run_b.metrics}
            for metric in run_a.metrics:
                other = b_metrics.get(metric.name)
                if other is not None and other.passed != metric.passed:
                    diffs.append(ScenarioDiff(
                        scenario_id=run_a.scenario_id,
                        metric=metric.name,
                        a_passed=metric.passed,
                        b_passed=other.passed,
                    ))
        return diffs

    @property
    def regressions(self) -> list[ScenarioDiff]:
        return [d for d in self.scenario_diffs if not d.b_passed]

    @property
    def as_dict(self) -> dict[str, Any]:
        return {
            "a": {"label": self.label_a, "mode": self.a.mode, "model": self.a.model},
            "b": {"label": self.label_b, "mode": self.b.mode, "model": self.b.model},
            "metric_deltas": {
                k: {"a": round(a, 4), "b": round(b, 4), "delta": round(d, 4)}
                for k, (a, b, d) in self.metric_deltas.items()
            },
            "scenario_diffs": [
                {"scenario": d.scenario_id, "metric": d.metric,
                 "a": d.a_passed, "b": d.b_passed, "verdict": d.verdict}
                for d in self.scenario_diffs
            ],
            "regression_count": len(self.regressions),
        }


def to_markdown(comparison: Comparison) -> str:
    lines = [
        "# APOE A/B Comparison",
        "",
        f"**A** — {comparison.label_a} (`{comparison.a.model}`)",
        f"**B** — {comparison.label_b} (`{comparison.b.model}`)",
        "",
        "## Metric deltas",
        "",
        "| Metric | A | B | Delta |",
        "|---|---|---|---|",
    ]
    deltas = comparison.metric_deltas
    for name in COLUMNS:
        if name not in deltas:
            continue
        a_mean, b_mean, delta = deltas[name]
        arrow = "=" if abs(delta) < 1e-9 else ("up" if delta > 0 else "DOWN")
        lines.append(
            f"| {name} | {a_mean:.0%} | {b_mean:.0%} | {delta:+.0%} {arrow} |"
        )

    diffs = comparison.scenario_diffs
    lines += ["", "## Per-scenario disagreements", ""]
    if not diffs:
        lines.append(
            "None — the two configurations agreed on every scenario and metric."
        )
    else:
        lines += ["| Scenario | Metric | A | B | Verdict |", "|---|---|---|---|---|"]
        for d in diffs:
            lines.append(
                f"| {d.scenario_id} | {d.metric} | "
                f"{'pass' if d.a_passed else 'FAIL'} | "
                f"{'pass' if d.b_passed else 'FAIL'} | {d.verdict} |"
            )

    regressions = comparison.regressions
    lines += [
        "",
        "## Verdict",
        "",
        (
            f"**{len(regressions)} regression(s)** — B fails scenarios A passed."
            if regressions
            else "**No regressions.** B does not fail anything A passed."
        ),
        "",
    ]
    return "\n".join(lines)


async def compare_models(
    model_a: str | None,
    model_b: str | None,
    base_url: str = "http://localhost:11434/v1",
    only: list[str] | None = None,
) -> Comparison:
    """Run the benchmark under two models and compare."""
    run_a = await run_benchmark(model=model_a, base_url=base_url, only=only)
    run_b = await run_benchmark(model=model_b, base_url=base_url, only=only)
    return Comparison(
        label_a=model_a or "rules-only baseline",
        label_b=model_b or "rules-only baseline",
        a=run_a, b=run_b,
    )


async def compare_prompts(
    prompt_a: str,
    prompt_b: str,
    model: str,
    base_url: str = "http://localhost:11434/v1",
    only: list[str] | None = None,
) -> Comparison:
    """Run the benchmark under two system prompts on the same model."""
    with prompt_variant(prompt_a):
        run_a = await run_benchmark(model=model, base_url=base_url, only=only)
    with prompt_variant(prompt_b):
        run_b = await run_benchmark(model=model, base_url=base_url, only=only)
    return Comparison(label_a="prompt A", label_b="prompt B", a=run_a, b=run_b)
