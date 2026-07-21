"""Domain models for the orchestrator.

Every payload that crosses a module boundary is a frozen-where-possible
``pydantic`` v2 model. The blackboard, agents, reasoner, safety compiler and
remediation engine all speak in these types — there are no untyped dicts on
the critical path.
"""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


def _utcnow() -> datetime:
    return datetime.now(tz=timezone.utc)


# ---------------------------------------------------------------------------
# Enumerations
# ---------------------------------------------------------------------------
class Severity(str, Enum):
    INFO = "INFO"
    WARNING = "WARNING"
    CRITICAL = "CRITICAL"


class SubsystemStatus(str, Enum):
    HEALTHY = "healthy"
    DEGRADED = "degraded"       # partial signal / data source unavailable
    FAULTED = "faulted"         # a real fault was observed
    UNAVAILABLE = "unavailable"  # the agent could not observe at all


class IncidentState(str, Enum):
    """Lifecycle states for an incident session (see ``blackboard``)."""

    CREATED = "created"
    DIAGNOSING = "diagnosing"
    DIAGNOSED = "diagnosed"
    PLANNING = "planning"
    REMEDIATING = "remediating"
    RESOLVED = "resolved"
    ESCALATED = "escalated"
    FAILED = "failed"


class RootCause(str, Enum):
    DB_LOCK_CONTENTION = "db_lock_contention"
    CPU_SATURATION = "cpu_saturation"
    MEMORY_LEAK = "memory_leak"
    CACHE_UNAVAILABLE = "cache_unavailable"
    KAFKA_CONSUMER_LAG = "kafka_consumer_lag"
    UNKNOWN = "unknown"


class ActionType(str, Enum):
    TERMINATE_BLOCKING_QUERIES = "terminate_blocking_queries"
    RESET_CHAOS_SANDBOX = "reset_chaos_sandbox"
    FLUSH_CACHE_KEY = "flush_cache_key"
    NOOP = "noop"


class RemediationStatus(str, Enum):
    APPLIED = "applied"
    SKIPPED_REPLAY = "skipped_replay"  # idempotency guard hit
    BLOCKED = "blocked"                # denied by safety policy
    DRY_RUN = "dry_run"
    FAILED = "failed"


# ---------------------------------------------------------------------------
# Core payloads
# ---------------------------------------------------------------------------
class Finding(BaseModel):
    """A single observation produced by one diagnostic agent."""

    model_config = ConfigDict(frozen=True)

    agent_name: str
    subsystem: str
    status: SubsystemStatus
    severity: Severity
    summary: str
    # Numeric evidence (e.g. blocking_backends=3, cpu_ratio=0.97).
    metrics: dict[str, float] = Field(default_factory=dict)
    # True when the data source was unreachable and this is a degraded payload.
    degraded: bool = False
    observed_at: datetime = Field(default_factory=_utcnow)

    @property
    def is_fault(self) -> bool:
        return self.status is SubsystemStatus.FAULTED


class ProposedAction(BaseModel):
    """A remediation the reasoner wants to take. Not yet safety-approved."""

    model_config = ConfigDict(frozen=True)

    action_type: ActionType
    target: str
    params: dict[str, Any] = Field(default_factory=dict)
    rationale: str = ""

    @property
    def idempotency_key(self) -> str:
        """Stable key used to guarantee replay-safety within a session.

        Two proposed actions with the same type + target + params collapse to
        one applied effect. The remediation engine records applied keys and
        skips replays.
        """
        items = ",".join(f"{k}={self.params[k]}" for k in sorted(self.params))
        return f"{self.action_type.value}:{self.target}:{items}"


class Diagnosis(BaseModel):
    """The reasoner's conclusion for an incident."""

    model_config = ConfigDict(frozen=True)

    root_cause: RootCause
    confidence: float = Field(ge=0.0, le=1.0)
    rationale: str
    evidence: list[str] = Field(default_factory=list)  # finding summaries
    proposed_actions: list[ProposedAction] = Field(default_factory=list)


class SafetyVerdict(BaseModel):
    model_config = ConfigDict(frozen=True)

    action: ProposedAction
    allowed: bool
    requires_approval: bool = False  # effect=approval_required: queue, don't run
    policy: str            # name of the deciding policy rule
    reason: str


class RemediationResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    action: ProposedAction
    status: RemediationStatus
    detail: str = ""
    executed_at: datetime = Field(default_factory=_utcnow)


class IncidentSession(BaseModel):
    """The blackboard record for one incident, mutated through its lifecycle."""

    incident_id: str
    trigger: str
    state: IncidentState = IncidentState.CREATED
    created_at: datetime = Field(default_factory=_utcnow)
    updated_at: datetime = Field(default_factory=_utcnow)
    trace_id: str | None = None

    findings: list[Finding] = Field(default_factory=list)
    diagnosis: Diagnosis | None = None
    verdicts: list[SafetyVerdict] = Field(default_factory=list)
    results: list[RemediationResult] = Field(default_factory=list)
