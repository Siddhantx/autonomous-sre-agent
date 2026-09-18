"""Execute the fault benchmark and score it.

Runs each scenario through the *real* Orchestrator — not a shortcut straight
into ``investigate`` — so the safety metrics observe genuine policy verdicts
and remediation results rather than a simulation of them. The default-deny
architecture is exercised, never bypassed.

Modes, and what each one honestly measures:

``rules-only``
    No LLM at all. Establishes the deterministic baseline and is the mode CI
    can run: free, offline, no judge. It proves the harness and the safety
    invariant, and it is *not* a measure of model capability.

``model``
    A real LLM (local via Ollama, or any configured provider). This is the
    only mode whose accuracy numbers describe a model.

There is deliberately no scripted-transcript mode here. Replaying a
pre-written answer measures the pipeline, and ``evals/run_evals.py`` already
covers that as the CI ceiling gate; putting it in the capability benchmark
would only invite quoting an oracle score as a model score.
"""

from __future__ import annotations

import asyncio
import sys
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import Any

_EVALS = Path(__file__).resolve().parents[1]
if str(_EVALS) not in sys.path:
    sys.path.insert(0, str(_EVALS))

from agent_orchestrator.agents import DiagnosticAgent  # noqa: E402
from agent_orchestrator.blackboard import Blackboard  # noqa: E402
from agent_orchestrator.config import Settings  # noqa: E402
from agent_orchestrator.investigator import LLMClient, make_llm_client  # noqa: E402
from agent_orchestrator.knowledge import KnowledgeStore, ingest_runbooks  # noqa: E402
from agent_orchestrator.models import (  # noqa: E402
    Finding,
    IncidentSession,
    InvestigationTrace,
)
from agent_orchestrator.orchestrator import Orchestrator  # noqa: E402

from .dataset import FaultBenchmark, ScenarioSpec, load_benchmark  # noqa: E402
from .metrics import (  # noqa: E402
    LatencyProfile,
    MetricResult,
    latency_profile,
    score_scenario,
)

RUNBOOKS = _EVALS.parent / "runbooks"


class _ReplayAgent(DiagnosticAgent):
    """Replays one dataset finding through the real agent machinery."""

    def __init__(self, finding: Finding) -> None:
        self.name = finding.agent_name
        self.subsystem = finding.subsystem
        self._finding = finding

    async def _observe(self, connectors: Any) -> Finding:
        return self._finding


class RecordedMutations:
    """The mutating half of the fake connector surface.

    ``fake_connectors`` is read-only, which is right for the diagnosis harness
    but leaves the remediation engine with nothing to call — so a scenario
    whose correct outcome is "take the whitelisted safe action" could never
    pass. These stand-ins let the action complete and record that it did, so
    the safe-action dimension is actually exercised.

    They are reached only *after* the default-deny policy has allowed the
    action, so recording a call here is evidence the safety engine approved
    it — never a bypass of it.
    """

    def __init__(self) -> None:
        self.calls: list[tuple[str, Any]] = []

    async def terminate_backend(self, pid: int) -> bool:
        self.calls.append(("terminate_backend", pid))
        return True

    async def delete_key(self, key: str) -> int:
        self.calls.append(("delete_key", key))
        return 1

    async def reset(self) -> dict[str, Any]:
        self.calls.append(("reset", None))
        return {"status": "reset"}


def connectors_for(spec: ScenarioSpec) -> tuple[Any, RecordedMutations]:
    """Synthetic connectors serving the scenario's ``tool_data``.

    Reuses the harness's existing read-only factory, which touches only
    ``.tool_data`` — so a namespace shim is enough and there is no second
    implementation to drift — then attaches the mutating surface on top.
    """
    from run_evals import fake_connectors  # local: evals/ is on sys.path

    # Deliberate duck-typing: fake_connectors touches only `.tool_data`, so a
    # namespace shim avoids re-implementing (and drifting from) its surface.
    ns = fake_connectors(SimpleNamespace(tool_data=spec.tool_data))  # type: ignore[arg-type]
    mutations = RecordedMutations()
    ns.postgres.terminate_backend = mutations.terminate_backend
    ns.redis.delete_key = mutations.delete_key
    ns.chaos = SimpleNamespace(reset=mutations.reset)
    return ns, mutations


@dataclass
class ScenarioRun:
    """One scenario executed once, with everything needed to report on it."""

    scenario_id: str
    group: str
    session: IncidentSession
    trace: InvestigationTrace
    metrics: list[MetricResult]
    latency: LatencyProfile
    wall_ms: float
    # Mutating connector calls that the safety engine allowed through.
    executed: list[tuple[str, Any]] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return all(m.passed for m in self.metrics)

    @property
    def as_dict(self) -> dict[str, Any]:
        diagnosis = self.session.diagnosis
        return {
            "scenario": self.scenario_id,
            "group": self.group,
            "passed": self.passed,
            "diagnosed_as": diagnosis.root_cause.value if diagnosis else None,
            "confidence": round(diagnosis.confidence, 3) if diagnosis else None,
            "final_state": self.session.state.value,
            "tools_called": self.trace.tools_called,
            "trace_outcome": self.trace.outcome,
            "tokens": self.trace.total_tokens,
            "wall_ms": round(self.wall_ms, 2),
            "latency": self.latency.as_dict,
            "metrics": [m.as_dict for m in self.metrics],
        }


@dataclass
class BenchmarkRun:
    """A full pass over the benchmark under one configuration."""

    mode: str
    model: str
    runs: list[ScenarioRun] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return all(r.passed for r in self.runs)


def build_settings(model: str | None, base_url: str) -> Settings:
    """Settings for one benchmark configuration.

    An empty ``llm_model`` is what disables the investigator, so rules-only
    mode is expressed by leaving it blank rather than by a separate flag.
    """
    return Settings(
        otel_enabled=False,
        audit_log_path=Path(tempfile.gettempdir()) / "apoe_llm_eval_audit.jsonl",
        llm_provider="openai",
        llm_base_url=base_url,
        llm_model=model or "",
        # Local models are slow; give them room rather than scoring a timeout.
        investigator_timeout_s=300.0 if model else 60.0,
    )


async def run_scenario(
    spec: ScenarioSpec,
    settings: Settings,
    llm: LLMClient | None = None,
) -> ScenarioRun:
    """Execute one scenario through the full pipeline and score it."""
    knowledge = KnowledgeStore()
    ingest_runbooks(knowledge, RUNBOOKS)
    for service, kind, summary, actor in (tuple(c) for c in spec.changes):
        knowledge.record_change(service, kind, summary, actor=actor)

    trace = InvestigationTrace()
    connectors, mutations = connectors_for(spec)
    orch = Orchestrator(
        settings,
        connectors,
        blackboard=Blackboard(),
        agents=[_ReplayAgent(f) for f in spec.to_findings()],
        llm=llm,
        knowledge=knowledge,
        trace=trace,
    )
    started = time.perf_counter()
    session = await orch.handle_incident(trigger=f"benchmark:{spec.id}")
    wall_ms = (time.perf_counter() - started) * 1000.0

    return ScenarioRun(
        scenario_id=spec.id,
        group=spec.group,
        session=session,
        trace=trace,
        metrics=score_scenario(session, trace, spec),
        latency=latency_profile(trace),
        wall_ms=wall_ms,
        executed=mutations.calls,
    )


def build_llm(settings: Settings, timeout_s: float = 600.0) -> LLMClient:
    """LLM client with a timeout suited to slow local models.

    ``OpenAICompatibleClient`` hardcodes a 30s HTTP timeout, which is sensible
    for a hosted API and far too short for a small model generating JSON on a
    CPU — the request aborts with ReadTimeout, the investigator escalates, and
    the benchmark silently scores a timeout instead of the model's reasoning.

    The constructor accepts an injected client, so this is fixed here in the
    eval layer rather than by changing the agent's production default.
    """
    if settings.llm_provider != "openai":
        return make_llm_client(settings)

    import httpx

    from agent_orchestrator.investigator import OpenAICompatibleClient

    headers = (
        {"Authorization": f"Bearer {settings.llm_api_key}"}
        if settings.llm_api_key
        else {}
    )
    return OpenAICompatibleClient(
        settings,
        client=httpx.AsyncClient(
            base_url=settings.llm_base_url, headers=headers, timeout=timeout_s
        ),
    )


async def run_benchmark(
    model: str | None = None,
    base_url: str = "http://localhost:11434/v1",
    benchmark: FaultBenchmark | None = None,
    only: list[str] | None = None,
) -> BenchmarkRun:
    """Run every scenario (or a subset) under one configuration."""
    bench = benchmark or load_benchmark()
    specs = [s for s in bench.scenarios if not only or s.id in only]
    settings = build_settings(model, base_url)
    llm = build_llm(settings) if model else None

    result = BenchmarkRun(
        mode="model" if model else "rules-only",
        model=model or "none (deterministic rules only)",
    )
    for spec in specs:
        result.runs.append(await run_scenario(spec, settings, llm))
    return result


def run_benchmark_sync(**kwargs: Any) -> BenchmarkRun:
    return asyncio.run(run_benchmark(**kwargs))
