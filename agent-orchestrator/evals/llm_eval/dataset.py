"""Load and validate the versioned fault benchmark.

The dataset is data, not code, so every field is validated on load against the
same enums the agent uses. A benchmark that silently accepts a misspelled root
cause would score a correct agent as wrong for the rest of its life, so all of
these are load-time errors rather than runtime surprises:

* ``expected.root_cause`` must be a real ``RootCause``
* ``expected.safe_actions`` must be real ``ActionType`` values — this is the
  whitelist the safety engine enforces, so a typo here would assert the wrong
  invariant
* ``expected.tools`` must name tools that actually exist in the registry
* scenario ids must be unique
"""

from __future__ import annotations

from collections import Counter
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from agent_orchestrator.investigator import TOOLS
from agent_orchestrator.models import (
    ActionType,
    Finding,
    RootCause,
    Severity,
    SubsystemStatus,
)

DEFAULT_DATASET = Path(__file__).resolve().parents[1] / "datasets" / "faults.yaml"

GROUPS = {"novel", "known", "adversarial"}


class FindingSpec(BaseModel):
    """One observation the diagnostic agents would surface."""

    model_config = ConfigDict(frozen=True)

    agent: str
    subsystem: str
    status: SubsystemStatus
    summary: str
    severity: Severity = Severity.INFO
    degraded: bool = False
    metrics: dict[str, float] = Field(default_factory=dict)

    def to_finding(self) -> Finding:
        return Finding(
            agent_name=self.agent,
            subsystem=self.subsystem,
            status=self.status,
            severity=self.severity,
            summary=self.summary,
            degraded=self.degraded,
            metrics=self.metrics,
        )


class ExpectedOutcome(BaseModel):
    """Ground truth for one scenario."""

    model_config = ConfigDict(frozen=True)

    root_cause: RootCause
    escalate: bool
    # Empty list means: no whitelisted action can safely resolve this, so the
    # only correct behaviour is escalation with cited evidence.
    safe_actions: list[ActionType] = Field(default_factory=list)
    # Scored as recall — extra probing is not penalised, missing the decisive
    # tool is. Empty list means no specific tool is required.
    tools: list[str] = Field(default_factory=list)

    @field_validator("tools")
    @classmethod
    def _tools_must_exist(cls, tools: list[str]) -> list[str]:
        unknown = sorted(set(tools) - set(TOOLS))
        if unknown:
            raise ValueError(
                f"unknown tool(s) {unknown}; registry has {sorted(TOOLS)}"
            )
        return tools


class ScenarioSpec(BaseModel):
    """One benchmark case: the world, and the answer it should produce."""

    model_config = ConfigDict(frozen=True)

    id: str
    group: str
    expected: ExpectedOutcome
    description: str = ""
    findings: list[FindingSpec] = Field(default_factory=list)
    # connector method name -> canned return value (e.g. "postgres.fetch")
    tool_data: dict[str, Any] = Field(default_factory=dict)
    # (service, change_kind, summary, actor) seeded into the knowledge store
    changes: list[list[str]] = Field(default_factory=list)

    @field_validator("group")
    @classmethod
    def _known_group(cls, group: str) -> str:
        if group not in GROUPS:
            raise ValueError(f"group {group!r} not in {sorted(GROUPS)}")
        return group

    @field_validator("changes")
    @classmethod
    def _changes_are_quads(cls, changes: list[list[str]]) -> list[list[str]]:
        for c in changes:
            if len(c) != 4:
                raise ValueError(
                    f"change {c!r} must be [service, kind, summary, actor]"
                )
        return changes

    def to_findings(self) -> list[Finding]:
        return [f.to_finding() for f in self.findings]


class FaultBenchmark(BaseModel):
    """The whole versioned dataset."""

    model_config = ConfigDict(frozen=True, populate_by_name=True)

    version: int
    scenarios: list[ScenarioSpec]
    schema_name: str = Field(default="", alias="schema")

    @model_validator(mode="after")
    def _unique_ids(self) -> FaultBenchmark:
        counts = Counter(s.id for s in self.scenarios)
        dupes = sorted(i for i, n in counts.items() if n > 1)
        if dupes:
            raise ValueError(f"duplicate scenario id(s): {dupes}")
        return self

    def by_group(self, group: str) -> list[ScenarioSpec]:
        return [s for s in self.scenarios if s.group == group]

    def get(self, scenario_id: str) -> ScenarioSpec:
        for s in self.scenarios:
            if s.id == scenario_id:
                return s
        raise KeyError(f"no scenario {scenario_id!r}")


def load_benchmark(path: Path | None = None) -> FaultBenchmark:
    """Load + validate the benchmark. Raises on any malformed field."""
    target = path or DEFAULT_DATASET
    raw = yaml.safe_load(target.read_text(encoding="utf-8"))
    return FaultBenchmark.model_validate(raw)
