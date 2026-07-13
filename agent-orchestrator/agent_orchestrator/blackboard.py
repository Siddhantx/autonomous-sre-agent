"""The Blackboard: shared incident state with a validated state machine.

Agents write findings concurrently; the reasoner reads them; the orchestrator
drives lifecycle transitions. All mutation goes through this class so that:

* state transitions are validated against an explicit legal-transition map
  (an illegal transition raises :class:`InvalidTransition` — never silently
  corrupts state), and
* concurrent finding appends from ``asyncio.gather``-ed agents are serialized
  by an ``asyncio.Lock``.

Storage is in-process (a dict). That is deliberate for V1.
"""

from __future__ import annotations

import asyncio

from .models import (
    Diagnosis,
    Finding,
    IncidentSession,
    IncidentState,
    RemediationResult,
    SafetyVerdict,
)
from .models import _utcnow

# Explicit legal transitions. Terminal states have no outgoing edges.
# ponytail: in-process map; move to a persisted saga log if sessions must
# survive orchestrator restarts.
_LEGAL_TRANSITIONS: dict[IncidentState, frozenset[IncidentState]] = {
    IncidentState.CREATED: frozenset({IncidentState.DIAGNOSING, IncidentState.FAILED}),
    IncidentState.DIAGNOSING: frozenset(
        {IncidentState.DIAGNOSED, IncidentState.FAILED}
    ),
    IncidentState.DIAGNOSED: frozenset(
        {
            IncidentState.PLANNING,
            IncidentState.RESOLVED,  # diagnosed healthy: nothing to remediate
            IncidentState.ESCALATED,
            IncidentState.FAILED,
        }
    ),
    IncidentState.PLANNING: frozenset(
        {IncidentState.REMEDIATING, IncidentState.ESCALATED, IncidentState.FAILED}
    ),
    IncidentState.REMEDIATING: frozenset(
        {IncidentState.RESOLVED, IncidentState.ESCALATED, IncidentState.FAILED}
    ),
    IncidentState.RESOLVED: frozenset(),
    IncidentState.ESCALATED: frozenset(),
    IncidentState.FAILED: frozenset(),
}


class InvalidTransition(RuntimeError):
    """Raised when an illegal incident state transition is attempted."""

    def __init__(self, frm: IncidentState, to: IncidentState) -> None:
        super().__init__(f"illegal transition {frm.value} -> {to.value}")
        self.frm = frm
        self.to = to


class UnknownIncident(KeyError):
    """Raised when an incident id is not present on the blackboard."""


def is_legal_transition(frm: IncidentState, to: IncidentState) -> bool:
    return to in _LEGAL_TRANSITIONS[frm]


class Blackboard:
    """Concurrency-safe store of :class:`IncidentSession` records."""

    def __init__(self) -> None:
        self._sessions: dict[str, IncidentSession] = {}
        self._lock = asyncio.Lock()

    def create(self, incident_id: str, trigger: str) -> IncidentSession:
        if incident_id in self._sessions:
            raise ValueError(f"incident {incident_id} already exists")
        session = IncidentSession(incident_id=incident_id, trigger=trigger)
        self._sessions[incident_id] = session
        return session

    def get(self, incident_id: str) -> IncidentSession:
        try:
            return self._sessions[incident_id]
        except KeyError as exc:
            raise UnknownIncident(incident_id) from exc

    def transition(self, incident_id: str, to: IncidentState) -> IncidentSession:
        """Move an incident to ``to`` iff the edge is legal, else raise."""
        session = self.get(incident_id)
        if not is_legal_transition(session.state, to):
            raise InvalidTransition(session.state, to)
        session.state = to
        session.updated_at = _utcnow()
        return session

    async def add_finding(self, incident_id: str, finding: Finding) -> None:
        async with self._lock:
            self.get(incident_id).findings.append(finding)

    def set_diagnosis(self, incident_id: str, diagnosis: Diagnosis) -> None:
        self.get(incident_id).diagnosis = diagnosis

    def add_verdict(self, incident_id: str, verdict: SafetyVerdict) -> None:
        self.get(incident_id).verdicts.append(verdict)

    def add_result(self, incident_id: str, result: RemediationResult) -> None:
        self.get(incident_id).results.append(result)

    def applied_idempotency_keys(self, incident_id: str) -> set[str]:
        """Keys already effected in this session (for replay-safety)."""
        from .models import RemediationStatus

        session = self.get(incident_id)
        return {
            r.action.idempotency_key
            for r in session.results
            if r.status in (RemediationStatus.APPLIED, RemediationStatus.DRY_RUN)
        }
