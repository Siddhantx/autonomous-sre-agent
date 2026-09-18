"""Evaluation reports: JSON for machines, markdown for humans.

Both carry the same mode banner, because the single most important thing a
reader needs is *what produced these numbers*. A rules-only baseline and a
real-model run produce the same shaped table and mean very different things,
and an accuracy figure quoted without its mode is worse than no figure.
"""

from __future__ import annotations

import json
import statistics
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .metrics import aggregate
from .runner import BenchmarkRun

# Metric columns in scorecard order: correctness, then safety, then process.
COLUMNS = [
    "root_cause_accuracy",
    "escalation_correctness",
    "unsafe_actions",
    "safe_action_compliance",
    "tool_selection_recall",
    "tool_call_validity",
    "fabrication",
]

_MODE_NOTE = {
    "rules-only": (
        "**Baseline — no LLM was involved.** These numbers describe the "
        "deterministic rule engine and the harness, not a model. Accuracy here "
        "is expected to be low: the novel-fault scenarios exist precisely "
        "because no rule covers them. What this mode does prove is that the "
        "pipeline runs and the safety invariant holds for free, offline, in CI."
    ),
    "model": (
        "**Real model run.** These accuracy numbers describe the configured "
        "model. They are not comparable to the scripted-LLM figures in "
        "`evals/RESULTS.md`, which are an architecture ceiling and CI gate "
        "rather than a measure of capability."
    ),
}


def _utc_now() -> str:
    return datetime.now(tz=timezone.utc).isoformat(timespec="seconds")


def to_json(result: BenchmarkRun) -> dict[str, Any]:
    """Machine-readable report."""
    means = aggregate([r.metrics for r in result.runs])
    latencies = [r.latency.total_ms for r in result.runs if r.latency.total_ms]
    unsafe_total = sum(
        0 if next(m for m in r.metrics if m.name == "unsafe_actions").passed else 1
        for r in result.runs
    )
    return {
        "generated_at": _utc_now(),
        "mode": result.mode,
        "model": result.model,
        "scenario_count": len(result.runs),
        "scenarios_passed": sum(1 for r in result.runs if r.passed),
        "all_passed": result.passed,
        "unsafe_action_scenarios": unsafe_total,
        "metric_means": {k: round(v, 4) for k, v in means.items()},
        "group_means": _group_means(result),
        "latency": {
            "mean_total_ms": round(statistics.mean(latencies), 2) if latencies else 0.0,
            "max_total_ms": round(max(latencies), 2) if latencies else 0.0,
        },
        "runs": [r.as_dict for r in result.runs],
    }


def _group_means(result: BenchmarkRun) -> dict[str, dict[str, float]]:
    groups: dict[str, list[list[Any]]] = {}
    for run in result.runs:
        groups.setdefault(run.group, []).append(run.metrics)
    return {
        group: {k: round(v, 4) for k, v in aggregate(metrics).items()}
        for group, metrics in sorted(groups.items())
    }


def _tick(passed: bool) -> str:
    return "pass" if passed else "FAIL"


def to_markdown(result: BenchmarkRun) -> str:
    """Human-readable scorecard."""
    means = aggregate([r.metrics for r in result.runs])
    passed = sum(1 for r in result.runs if r.passed)
    unsafe_clean = all(
        next(m for m in r.metrics if m.name == "unsafe_actions").passed
        for r in result.runs
    )

    lines = [
        "# APOE GenAI Evaluation Scorecard",
        "",
        f"Mode: **{result.mode}** · model: **{result.model}** · "
        f"generated {_utc_now()}",
        "",
        _MODE_NOTE.get(result.mode, ""),
        "",
        "## Headline",
        "",
        f"- Scenarios passing every metric: **{passed}/{len(result.runs)}**",
        f"- Unsafe actions: **{'0 — invariant holds' if unsafe_clean else 'VIOLATED'}**",
        "",
        "## Metric means",
        "",
        "| Metric | Mean |",
        "|---|---|",
    ]
    for name in COLUMNS:
        if name in means:
            lines.append(f"| {name} | {means[name]:.0%} |")

    lines += ["", "## By group", "", "| Group | " + " | ".join(COLUMNS) + " |",
              "|---" * (len(COLUMNS) + 1) + "|"]
    for group, gmeans in _group_means(result).items():
        row = " | ".join(f"{gmeans.get(c, 0.0):.0%}" for c in COLUMNS)
        lines.append(f"| {group} | {row} |")

    lines += [
        "",
        "## Per scenario",
        "",
        "| Scenario | Group | Diagnosed as | Final state | " + " | ".join(COLUMNS) + " |",
        "|---" * (len(COLUMNS) + 4) + "|",
    ]
    for run in result.runs:
        by_name = {m.name: m for m in run.metrics}
        diagnosis = run.session.diagnosis
        dx = diagnosis.root_cause.value if diagnosis else "-"
        cells = " | ".join(_tick(by_name[c].passed) for c in COLUMNS if c in by_name)
        lines.append(
            f"| {run.scenario_id} | {run.group} | {dx} | "
            f"{run.session.state.value} | {cells} |"
        )

    failures = [
        (r.scenario_id, m)
        for r in result.runs for m in r.metrics if not m.passed
    ]
    if failures:
        lines += ["", "## Failures in detail", ""]
        for scenario_id, metric in failures:
            lines.append(f"- **{scenario_id}** / `{metric.name}` — {metric.detail}")

    lines += [
        "",
        "## Latency",
        "",
        "| Scenario | Steps | Total ms | LLM mean ms | Tool mean ms |",
        "|---|---|---|---|---|",
    ]
    for run in result.runs:
        lat = run.latency
        lines.append(
            f"| {run.scenario_id} | {lat.steps} | {lat.total_ms:.1f} | "
            f"{lat.llm_mean_ms:.1f} | {lat.tool_mean_ms:.1f} |"
        )

    lines.append("")
    return "\n".join(lines)


def write_report(result: BenchmarkRun, out_dir: Path, stem: str = "") -> tuple[Path, Path]:
    """Write both report formats; returns (json_path, markdown_path)."""
    out_dir.mkdir(parents=True, exist_ok=True)
    name = stem or f"scorecard-{result.mode}"
    json_path = out_dir / f"{name}.json"
    md_path = out_dir / f"{name}.md"
    json_path.write_text(
        json.dumps(to_json(result), indent=2) + "\n", encoding="utf-8"
    )
    md_path.write_text(to_markdown(result), encoding="utf-8")
    return json_path, md_path
