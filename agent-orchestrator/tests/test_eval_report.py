"""Scorecard reports.

The property that matters most: a report must never present a number without
saying what produced it. A rules-only baseline and a real-model run make the
same shaped table and mean very different things.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "evals"))

from llm_eval.report import to_json, to_markdown, write_report  # noqa: E402
from llm_eval.runner import run_benchmark  # noqa: E402


@pytest.fixture(scope="module")
def baseline():
    import asyncio

    return asyncio.run(run_benchmark(only=["healthy-noise", "db-lock-contention",
                                           "two-faults-at-once"]))


def test_json_report_carries_mode_and_per_run_detail(baseline) -> None:
    payload = to_json(baseline)
    assert payload["mode"] == "rules-only"
    assert payload["scenario_count"] == 3
    assert "metric_means" in payload
    assert "group_means" in payload
    assert len(payload["runs"]) == 3
    # every run reports its metrics and latency
    assert all("metrics" in r and "latency" in r for r in payload["runs"])
    # serialisable
    json.dumps(payload)


def test_markdown_states_what_produced_the_numbers(baseline) -> None:
    md = to_markdown(baseline)
    assert "Mode: **rules-only**" in md
    assert "no LLM was involved" in md
    # and warns against comparing to the scripted ceiling
    assert "not a model" in md


def test_markdown_surfaces_the_safety_headline(baseline) -> None:
    md = to_markdown(baseline)
    assert "Unsafe actions" in md
    assert "invariant holds" in md


def test_markdown_explains_each_failure(baseline) -> None:
    """A scorecard of bare FAILs is not actionable."""
    md = to_markdown(baseline)
    failing = [m for r in baseline.runs for m in r.metrics if not m.passed]
    if failing:
        assert "Failures in detail" in md
        assert failing[0].detail[:20] in md


def test_write_report_emits_both_formats(baseline, tmp_path: Path) -> None:
    json_path, md_path = write_report(baseline, tmp_path)
    assert json_path.exists() and md_path.exists()
    assert json.loads(json_path.read_text(encoding="utf-8"))["mode"] == "rules-only"
    assert md_path.read_text(encoding="utf-8").startswith("# APOE GenAI Evaluation")


def test_unsafe_scenario_count_is_reported(baseline) -> None:
    payload = to_json(baseline)
    assert payload["unsafe_action_scenarios"] == 0
