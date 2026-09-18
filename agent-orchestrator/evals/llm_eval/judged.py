"""LLM-judged metrics via DeepEval — the opt-in tier.

Why DeepEval rather than RAGAS: APOE is an *agent*, not a RAG pipeline. RAGAS
models the world as (question, contexts, answer, ground_truth) and has no
concept of a tool call, so tool-call correctness and reasoning-quality over a
ReAct trace are unexpressible in it. DeepEval ships ToolCorrectnessMetric as a
first-class metric and GEval for arbitrary judged rubrics, and it is
pytest-native. APOE's knowledge layer is FTS5 lexical search with no
embeddings at all, so RAGAS's specialty — embedding-based context
precision/recall — would be measuring something this system does not do.

**This module is optional by design.** DeepEval upgrades opentelemetry-sdk
past the version the agent pins, so it is installed in its own environment
(see requirements-eval.txt). Importing this module without DeepEval present
raises a clear, actionable error rather than a bare ImportError, and nothing
else in the package imports it at module scope — the deterministic metrics
that gate CI never touch it.

**Judge quality is a first-class caveat.** Scoring a 3B agent with a 3B judge
is a weak judge, and the report says so rather than presenting the numbers as
authoritative. The judge is configurable precisely so that claim can be
strengthened by pointing it at a better model.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

_EVALS = Path(__file__).resolve().parents[1]
if str(_EVALS) not in sys.path:
    sys.path.insert(0, str(_EVALS))

from agent_orchestrator.models import Diagnosis, InvestigationTrace  # noqa: E402

from .metrics import MetricResult  # noqa: E402

_INSTALL_HINT = (
    "The judged metrics tier needs DeepEval, which is intentionally not in "
    "requirements.txt (it upgrades opentelemetry-sdk past the version this "
    "project pins). Install it in its own environment:\n"
    "    python -m venv .venv-eval\n"
    "    .venv-eval/Scripts/pip install -r requirements.txt -r requirements-eval.txt\n"
    "The deterministic metrics in llm_eval.metrics need none of this."
)


def require_deepeval() -> Any:
    """Import DeepEval or explain precisely how to get it."""
    try:
        import deepeval  # noqa: F401
    except ImportError as exc:  # pragma: no cover - exercised in the eval env
        raise ImportError(_INSTALL_HINT) from exc
    return deepeval


def deepeval_available() -> bool:
    try:
        import deepeval  # noqa: F401
    except ImportError:
        return False
    return True


# ---------------------------------------------------------------------------
# Judge model adapter
# ---------------------------------------------------------------------------
def build_judge(model: str, base_url: str = "http://localhost:11434/v1") -> Any:
    """Wrap an OpenAI-compatible endpoint as a DeepEval judge.

    Defaults to a local Ollama endpoint so the judged tier costs nothing and
    runs air-gapped, consistent with the rest of the project. Point it at a
    stronger endpoint to strengthen the judgements.
    """
    require_deepeval()
    from deepeval.models import GPTModel

    return GPTModel(model=model, base_url=base_url, _openai_api_key="not-needed")


# ---------------------------------------------------------------------------
# Mapping APOE -> DeepEval
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class JudgedCase:
    """One investigation, shaped for DeepEval.

    The mapping is the interesting part, and it is deliberately explicit:

    ``input``            what the agent was asked to explain (the incident)
    ``actual_output``    the diagnosis it produced, as prose
    ``retrieval_context`` everything it actually saw — seeded findings,
                         knowledge hits, and every tool result. Faithfulness
                         and hallucination are scored against *this*, not
                         against ground truth, because the question is whether
                         the conclusion follows from the evidence the agent
                         had, not whether it happened to be right.
    """

    input: str
    actual_output: str
    retrieval_context: list[str]
    reasoning_trace: str

    def to_test_case(self) -> Any:
        require_deepeval()
        from deepeval.test_case import LLMTestCase

        return LLMTestCase(
            input=self.input,
            actual_output=self.actual_output,
            retrieval_context=self.retrieval_context,
            context=self.retrieval_context,
        )


def diagnosis_as_text(diagnosis: Diagnosis | None) -> str:
    if diagnosis is None:
        return "No diagnosis was produced."
    parts = [
        f"Root cause: {diagnosis.root_cause.value} "
        f"(confidence {diagnosis.confidence:.2f}).",
        f"Rationale: {diagnosis.rationale}",
    ]
    if diagnosis.evidence:
        parts.append("Evidence: " + "; ".join(diagnosis.evidence))
    if diagnosis.hypotheses:
        parts.append(
            "Considered: "
            + "; ".join(
                f"{h.root_cause.value} @ {h.confidence:.2f}"
                for h in diagnosis.hypotheses
            )
        )
    return "\n".join(parts)


def trace_as_text(trace: InvestigationTrace) -> str:
    """Flatten the ReAct trace into something a judge can reason about."""
    lines: list[str] = []
    for step in trace.steps:
        lines.append(f"Step {step.step} ({step.action}):")
        for call in step.tool_calls:
            status = "ok" if call.ok else "ERROR"
            lines.append(
                f"  called {call.tool}({call.args}) -> {status}: "
                f"{call.result[:300]}"
            )
        if step.action == "diagnose":
            lines.append(f"  concluded: {step.raw_response[:500]}")
    return "\n".join(lines) or "No reasoning steps were recorded."


def build_case(
    incident_summary: str, diagnosis: Diagnosis | None, trace: InvestigationTrace
) -> JudgedCase:
    return JudgedCase(
        input=incident_summary,
        actual_output=diagnosis_as_text(diagnosis),
        retrieval_context=trace.retrieval_context or ["No evidence was gathered."],
        reasoning_trace=trace_as_text(trace),
    )


# ---------------------------------------------------------------------------
# Metrics
# ---------------------------------------------------------------------------
REASONING_RUBRIC = (
    "Judge whether the diagnosis follows soundly from the evidence actually "
    "gathered in the reasoning trace. Reward: conclusions supported by "
    "specific cited evidence; considering and ruling out alternatives; "
    "admitting uncertainty when the evidence is thin or the data sources were "
    "unavailable. Penalise: asserting a cause no evidence supports; ignoring "
    "contradictory evidence; confident conclusions drawn from failed or empty "
    "tool calls. An honest 'I cannot determine this' on weak evidence is a "
    "GOOD answer and must score highly, not poorly."
)


def build_metrics(judge: Any, threshold: float = 0.5) -> list[Any]:
    """The judged metric set: faithfulness, relevancy, hallucination, reasoning."""
    require_deepeval()
    from deepeval.metrics import (
        AnswerRelevancyMetric,
        FaithfulnessMetric,
        GEval,
        HallucinationMetric,
    )
    from deepeval.test_case import LLMTestCaseParams

    return [
        FaithfulnessMetric(threshold=threshold, model=judge, include_reason=True),
        AnswerRelevancyMetric(threshold=threshold, model=judge, include_reason=True),
        HallucinationMetric(threshold=threshold, model=judge, include_reason=True),
        GEval(
            name="reasoning_quality",
            criteria=REASONING_RUBRIC,
            evaluation_params=[
                LLMTestCaseParams.INPUT,
                LLMTestCaseParams.ACTUAL_OUTPUT,
                LLMTestCaseParams.CONTEXT,
            ],
            threshold=threshold,
            model=judge,
        ),
    ]


def judge_case(case: JudgedCase, metrics: list[Any]) -> list[MetricResult]:
    """Score one case, returning results in the same shape as the
    deterministic tier so both can share a scorecard."""
    test_case = case.to_test_case()
    results: list[MetricResult] = []
    for metric in metrics:
        metric.measure(test_case)
        score = float(metric.score or 0.0)
        # HallucinationMetric is inverted: higher score = more hallucination.
        name = getattr(metric, "__name__", metric.__class__.__name__)
        normalised = 1.0 - score if "Hallucination" in metric.__class__.__name__ else score
        results.append(MetricResult(
            name=_metric_name(metric),
            score=normalised,
            passed=bool(metric.is_successful()),
            detail=str(getattr(metric, "reason", "") or name),
            threshold=float(getattr(metric, "threshold", 0.5)),
        ))
    return results


def _metric_name(metric: Any) -> str:
    raw = getattr(metric, "name", None) or metric.__class__.__name__
    return str(raw).lower().replace(" ", "_").replace("metric", "").strip("_")
