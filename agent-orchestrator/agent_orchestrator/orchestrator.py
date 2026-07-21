"""The Active Agent Orchestrator — the incident session pipeline.

One incident = one distributed trace. The pipeline drives the blackboard state
machine through: observe (fan-out agents) -> diagnose (reasoner) -> plan
(safety compiler) -> remediate (idempotent engine), transitioning to RESOLVED,
ESCALATED or FAILED.

Failure isolation: agent failures degrade (they never reach here as
exceptions); a reasoner/safety/remediation exception fails *this incident* and
transitions it to FAILED, but never crashes the process.
"""

from __future__ import annotations

import uuid

from opentelemetry import trace

from .agents import DiagnosticAgent, default_agents, run_all
from .approvals import ApprovalQueue
from .audit import audit_event
from .blackboard import Blackboard
from .config import Settings
from .connectors import Connectors
from .investigator import LLMClient, investigate, make_llm_client
from .knowledge import KnowledgeStore
from .models import IncidentSession, IncidentState, RemediationStatus, RootCause
from .observability import bind_incident, clear_context, get_logger, get_tracer
from .reasoner import reason
from .remediation import RemediationEngine
from .safety import CompiledPolicy, load_policy

log = get_logger("orchestrator")


class Orchestrator:
    def __init__(
        self,
        settings: Settings,
        connectors: Connectors,
        blackboard: Blackboard | None = None,
        agents: list[DiagnosticAgent] | None = None,
        policy: CompiledPolicy | None = None,
        llm: LLMClient | None = None,
        knowledge: KnowledgeStore | None = None,
        approvals: ApprovalQueue | None = None,
    ) -> None:
        self._settings = settings
        self._connectors = connectors
        self.blackboard = blackboard or Blackboard()
        self._agents = agents if agents is not None else default_agents()
        self._policy = policy or load_policy(settings.safety_policy_path)
        self.remediation = RemediationEngine(settings, connectors)
        # Investigator is disabled unless a model is configured or injected.
        self._llm = llm or (make_llm_client(settings) if settings.llm_model else None)
        self.knowledge = knowledge
        self._approvals = approvals

    async def handle_incident(self, trigger: str) -> IncidentSession:
        """Run the full pipeline for a new incident and return the record."""
        incident_id = f"inc-{uuid.uuid4().hex[:12]}"
        bind_incident(incident_id)
        tracer = get_tracer()
        with tracer.start_as_current_span("incident.session") as span:
            span.set_attribute("incident.id", incident_id)
            span.set_attribute("incident.trigger", trigger)
            session = self.blackboard.create(incident_id, trigger)
            ctx = span.get_span_context()
            session.trace_id = format(ctx.trace_id, "032x")
            log.info("incident_opened", trigger=trigger)
            try:
                await self._diagnose(session)
                await self._plan_and_remediate(session)
            except Exception as exc:  # incident-scoped failure isolation
                log.error(
                    "incident_failed", error=str(exc), error_type=type(exc).__name__
                )
                span.record_exception(exc)
                span.set_status(trace.Status(trace.StatusCode.ERROR))
                if session.state not in (
                    IncidentState.RESOLVED,
                    IncidentState.ESCALATED,
                    IncidentState.FAILED,
                ):
                    self.blackboard.transition(incident_id, IncidentState.FAILED)
            finally:
                # Learning loop: every terminal outcome becomes a post-mortem.
                if self.knowledge is not None:
                    try:
                        self.knowledge.add_post_mortem(session)
                    except Exception as exc:
                        log.warning("post_mortem_failed", error=str(exc))
                log.info("incident_closed", final_state=session.state.value)
                clear_context()
            return session

    async def _diagnose(self, session: IncidentSession) -> None:
        self.blackboard.transition(session.incident_id, IncidentState.DIAGNOSING)
        findings = await run_all(self._agents, self._connectors, self._settings)
        for finding in findings:
            await self.blackboard.add_finding(session.incident_id, finding)
        diagnosis = reason(findings)
        if self._llm is not None and (
            diagnosis.root_cause is RootCause.UNKNOWN
            or diagnosis.confidence < self._settings.investigator_threshold
        ):
            log.info(
                "investigator_activated",
                rule_root_cause=diagnosis.root_cause.value,
                rule_confidence=diagnosis.confidence,
            )
            llm_diagnosis = await investigate(
                session, self.blackboard, self._connectors, self._settings,
                self._llm, self.knowledge,
            )
            # Keep the better of the two; investigate() never raises.
            if llm_diagnosis.confidence >= diagnosis.confidence:
                diagnosis = llm_diagnosis
        self.blackboard.set_diagnosis(session.incident_id, diagnosis)
        self.blackboard.transition(session.incident_id, IncidentState.DIAGNOSED)
        log.info(
            "incident_diagnosed",
            root_cause=diagnosis.root_cause.value,
            confidence=diagnosis.confidence,
            proposed_actions=len(diagnosis.proposed_actions),
        )

    async def _plan_and_remediate(self, session: IncidentSession) -> None:
        diagnosis = session.diagnosis
        assert diagnosis is not None
        incident_id = session.incident_id

        if not diagnosis.proposed_actions:
            # Healthy only if nothing faulted AND no root cause was named;
            # a diagnosed cause with no safe automatic action must escalate.
            target = (
                IncidentState.RESOLVED
                if diagnosis.root_cause is RootCause.UNKNOWN
                and not any(f.is_fault for f in session.findings)
                else IncidentState.ESCALATED
            )
            self.blackboard.transition(incident_id, target)
            log.info("incident_no_action", outcome=target.value)
            return

        self.blackboard.transition(incident_id, IncidentState.PLANNING)
        approved, queued = [], []
        for action in diagnosis.proposed_actions:
            verdict = self._policy.evaluate(action, diagnosis.confidence)
            self.blackboard.add_verdict(incident_id, verdict)
            audit_event(
                self._settings, "proposed",
                incident_id=incident_id,
                action_type=action.action_type.value,
                rationale=action.rationale,
                policy=verdict.policy,
                allowed=verdict.allowed,
                requires_approval=verdict.requires_approval,
            )
            log.info(
                "safety_verdict",
                action=action.action_type.value,
                allowed=verdict.allowed,
                requires_approval=verdict.requires_approval,
                policy=verdict.policy,
            )
            if verdict.allowed:
                approved.append(action)
            elif verdict.requires_approval and self._approvals is not None:
                queued.append(
                    self._approvals.enqueue(incident_id, action, diagnosis.confidence)
                )

        if queued:
            log.info(
                "approvals_queued",
                approval_ids=[q.approval_id for q in queued],
            )

        if not approved:
            self.blackboard.transition(incident_id, IncidentState.ESCALATED)
            log.info(
                "incident_blocked_by_policy", pending_approvals=len(queued)
            )
            return

        self.blackboard.transition(incident_id, IncidentState.REMEDIATING)
        for action in approved:
            applied = self.blackboard.applied_idempotency_keys(incident_id)
            result = await self.remediation.execute(action, applied)
            self.blackboard.add_result(incident_id, result)
            audit_event(
                self._settings, "executed",
                incident_id=incident_id,
                action_type=action.action_type.value,
                rationale=action.rationale,
                status=result.status.value,
                detail=result.detail,
            )

        failed = any(
            r.status is RemediationStatus.FAILED for r in session.results
        )
        self.blackboard.transition(
            incident_id,
            IncidentState.ESCALATED if failed else IncidentState.RESOLVED,
        )
        log.info(
            "incident_remediated",
            outcome=session.state.value,
            results=[r.status.value for r in session.results],
        )
