"""FastAPI entrypoint: trigger incidents and run the end-to-end simulation.

Endpoints:
    GET  /health                       liveness (open)
    GET  /incidents/{id}               fetch a session record (open)
    GET  /approvals                    list pending approvals (open)
    POST /incidents                    run the pipeline (API key)
    POST /simulate/{scenario}          inject chaos fault + run (API key)
    POST /approvals/{id}/approve       execute a queued action (API key)
    POST /approvals/{id}/reject        reject with a recorded reason (API key)

Mutating endpoints require the ``X-API-Key`` header matching ``APOE_API_KEY``.
No key configured = every mutating request is rejected (default-deny).
"""

from __future__ import annotations

import asyncio
import secrets
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any, cast

from fastapi import Depends, FastAPI, Header, HTTPException
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from pydantic import BaseModel, Field

from .approvals import (
    AlreadyResolved,
    ApprovalQueue,
    PendingApproval,
    promotion_candidates,
)
from .audit import audit_event
from .blackboard import Blackboard, UnknownIncident
from .config import get_settings
from .connectors import Connectors
from .knowledge import KnowledgeStore, ingest_all
from .models import IncidentSession, RemediationResult
from .observability import configure_observability, get_logger
from .orchestrator import Orchestrator
from .ui import router as ui_router

log = get_logger("api")


def require_api_key(
    x_api_key: str = Header(default="", alias="X-API-Key")
) -> None:
    expected = get_settings().api_key
    if not expected or not secrets.compare_digest(x_api_key, expected):
        raise HTTPException(status_code=401, detail="invalid or missing API key")


class TriggerRequest(BaseModel):
    trigger: str = "manual-trigger"


class WebhookAlert(BaseModel):
    """Inbound alert from PagerDuty / Alertmanager / any webhook source."""
    source: str = "unknown"
    summary: str = ""
    severity: str = "warning"
    labels: dict[str, str] = Field(default_factory=dict)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    configure_observability(settings)
    connectors = Connectors(settings)
    knowledge = KnowledgeStore(settings.knowledge_db_path)
    log.info("knowledge_ingested", **ingest_all(knowledge, settings))
    app.state.settings = settings
    app.state.connectors = connectors
    app.state.approvals = ApprovalQueue()
    app.state.orchestrator = Orchestrator(
        settings, connectors, blackboard=Blackboard(), knowledge=knowledge,
        approvals=app.state.approvals,
    )
    log.info("orchestrator_started", environment=settings.environment)
    try:
        yield
    finally:
        await connectors.aclose()
        knowledge.close()
        log.info("orchestrator_stopped")


app = FastAPI(title="APOE Active Agent Orchestrator", version="1.0", lifespan=lifespan)
app.include_router(ui_router)
FastAPIInstrumentor.instrument_app(app)


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "healthy", "service": "agent-orchestrator"}


@app.post("/incidents", dependencies=[Depends(require_api_key)])
async def create_incident(req: TriggerRequest) -> IncidentSession:
    orchestrator: Orchestrator = app.state.orchestrator
    return await orchestrator.handle_incident(req.trigger)


@app.get("/incidents/{incident_id}")
async def get_incident(incident_id: str) -> IncidentSession:
    orchestrator: Orchestrator = app.state.orchestrator
    try:
        return orchestrator.blackboard.get(incident_id)
    except UnknownIncident:
        raise HTTPException(status_code=404, detail="incident not found")


_SCENARIOS = {"db-lock", "high-cpu", "leak"}


@app.post("/simulate/{scenario}", dependencies=[Depends(require_api_key)])
async def simulate(scenario: str) -> dict[str, Any]:
    """Inject a fault via the chaos-injector, wait for it to take, then respond."""
    if scenario not in _SCENARIOS:
        raise HTTPException(status_code=400, detail=f"unknown scenario '{scenario}'")
    connectors: Connectors = app.state.connectors
    orchestrator: Orchestrator = app.state.orchestrator

    injected = await connectors.chaos.trigger(scenario)
    # Give the fault a moment to become observable (the db lock, cpu loop, etc.)
    await asyncio.sleep(3.0)
    session = await orchestrator.handle_incident(trigger=f"chaos:{scenario}")
    return {"injected": injected, "incident": session.model_dump(mode="json")}


# ---------------------------------------------------------------------------
# Change events ("what changed?") — CI/CD systems POST here on every deploy
# ---------------------------------------------------------------------------
class ChangeEvent(BaseModel):
    service: str
    change_kind: str = "deploy"   # deploy | config | schema | infra | commit
    summary: str
    actor: str = "unknown"


@app.post("/changes", dependencies=[Depends(require_api_key)])
async def record_change(event: ChangeEvent) -> dict[str, str]:
    orchestrator: Orchestrator = app.state.orchestrator
    knowledge = orchestrator.knowledge
    if knowledge is None:
        raise HTTPException(status_code=503, detail="knowledge store not configured")
    change_id = knowledge.record_change(
        event.service, event.change_kind, event.summary, actor=event.actor
    )
    log.info("change_recorded", change_id=change_id, service=event.service,
             change_kind=event.change_kind)
    return {"change_id": change_id}


@app.get("/changes")
async def list_changes(service: str | None = None) -> list[dict[str, Any]]:
    orchestrator: Orchestrator = app.state.orchestrator
    knowledge = orchestrator.knowledge
    if knowledge is None:
        return []
    return knowledge.recent_changes(20, service=service)


# ---------------------------------------------------------------------------
# Human-approval workflow
# ---------------------------------------------------------------------------
class RejectRequest(BaseModel):
    reason: str
    actor: str = "human"


class ApproveRequest(BaseModel):
    actor: str = "human"


@app.get("/approvals")
async def list_approvals() -> list[PendingApproval]:
    return cast(list[PendingApproval], app.state.approvals.pending())


@app.post("/approvals/{approval_id}/approve", dependencies=[Depends(require_api_key)])
async def approve_action(
    approval_id: str, req: ApproveRequest | None = None
) -> RemediationResult:
    """Approve a queued action; it executes through the idempotent engine."""
    queue: ApprovalQueue = app.state.approvals
    orchestrator: Orchestrator = app.state.orchestrator
    settings = app.state.settings
    actor = req.actor if req else "human"
    try:
        item = queue.resolve(approval_id, "approved")
    except KeyError:
        raise HTTPException(status_code=404, detail="approval not found")
    except AlreadyResolved as exc:
        raise HTTPException(status_code=409, detail=str(exc))

    audit_event(
        settings, "approved",
        incident_id=item.incident_id,
        action_type=item.action.action_type.value,
        rationale=item.action.rationale,
        actor=actor,
        approval_id=approval_id,
        target=item.action.target,
    )
    applied = orchestrator.blackboard.applied_idempotency_keys(item.incident_id)
    result = await orchestrator.remediation.execute(item.action, applied)
    orchestrator.blackboard.add_result(item.incident_id, result)
    audit_event(
        settings, "executed",
        incident_id=item.incident_id,
        action_type=item.action.action_type.value,
        rationale=item.action.rationale,
        actor=actor,
        status=result.status.value,
        detail=result.detail,
    )
    return result


@app.post("/approvals/{approval_id}/reject", dependencies=[Depends(require_api_key)])
async def reject_action(approval_id: str, req: RejectRequest) -> PendingApproval:
    queue: ApprovalQueue = app.state.approvals
    settings = app.state.settings
    try:
        item = queue.resolve(approval_id, "rejected", reason=req.reason)
    except KeyError:
        raise HTTPException(status_code=404, detail="approval not found")
    except AlreadyResolved as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    audit_event(
        settings, "rejected",
        incident_id=item.incident_id,
        action_type=item.action.action_type.value,
        rationale=req.reason,
        actor=req.actor,
        approval_id=approval_id,
        target=item.action.target,
    )
    return item


@app.get("/policy/suggestions")
async def policy_suggestions() -> list[dict[str, Any]]:
    """Auto-allow candidates from repeated human approvals. Read-only —
    a human reviews the suggested YAML and merges it into policies.yaml."""
    return promotion_candidates(app.state.settings)


# ---------------------------------------------------------------------------
# Inbound alert webhook (PagerDuty / Alertmanager / generic)
# ---------------------------------------------------------------------------
@app.post("/webhooks/alert", dependencies=[Depends(require_api_key)])
async def alert_webhook(alert: WebhookAlert) -> IncidentSession:
    """Accept an external alert and run the full incident pipeline."""
    trigger = f"webhook:{alert.source}:{alert.summary[:80]}"
    orchestrator: Orchestrator = app.state.orchestrator
    log.info("webhook_received", source=alert.source, severity=alert.severity)
    return await orchestrator.handle_incident(trigger)
