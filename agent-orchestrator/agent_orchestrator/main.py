"""FastAPI entrypoint: trigger incidents and run the end-to-end simulation.

Endpoints:
    GET  /health                 liveness
    POST /incidents              run the pipeline against live subsystems
    GET  /incidents/{id}         fetch a session record
    POST /simulate/{scenario}    inject a chaos fault, then run an incident
                                 (scenario in: db-lock, high-cpu, leak)
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, HTTPException
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from pydantic import BaseModel

from .blackboard import Blackboard, UnknownIncident
from .config import get_settings
from .connectors import Connectors
from .models import IncidentSession
from .observability import configure_observability, get_logger
from .orchestrator import Orchestrator

log = get_logger("api")


class TriggerRequest(BaseModel):
    trigger: str = "manual-trigger"


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    configure_observability(settings)
    connectors = Connectors(settings)
    app.state.settings = settings
    app.state.connectors = connectors
    app.state.orchestrator = Orchestrator(
        settings, connectors, blackboard=Blackboard()
    )
    log.info("orchestrator_started", environment=settings.environment)
    try:
        yield
    finally:
        await connectors.aclose()
        log.info("orchestrator_stopped")


app = FastAPI(title="APOE Active Agent Orchestrator", version="1.0", lifespan=lifespan)
FastAPIInstrumentor.instrument_app(app)


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "healthy", "service": "agent-orchestrator"}


@app.post("/incidents")
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


@app.post("/simulate/{scenario}")
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
