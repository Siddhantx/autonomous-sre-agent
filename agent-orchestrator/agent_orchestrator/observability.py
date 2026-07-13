"""Structured logging (structlog, JSON) and OpenTelemetry (traces + metrics).

Two concerns, one module because they share a lifecycle: both are configured
once at process start and both bind incident/agent context.

Logging contract (required on every record): ``incident_id``, ``agent_name``,
``timestamp``, ``severity``. These are injected by processors and by
``contextvars`` bound via :func:`bind_incident` / :func:`bind_agent`, so callers
never have to remember them.
"""

from __future__ import annotations

import logging
from typing import Any

import structlog
from opentelemetry import metrics, trace
from opentelemetry.exporter.otlp.proto.grpc.metric_exporter import (
    OTLPMetricExporter,
)
from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor

from .config import Settings

_CONFIGURED = False


def _add_severity(_: Any, method: str, event: dict[str, Any]) -> dict[str, Any]:
    """Map structlog level to the required ``severity`` field."""
    event.setdefault("severity", event.get("level", method).upper())
    return event


def _ensure_required_context(_: Any, __: str, event: dict[str, Any]) -> dict[str, Any]:
    """Guarantee the mandated keys are always present (defaults if unbound)."""
    event.setdefault("incident_id", "-")
    event.setdefault("agent_name", "orchestrator")
    return event


def configure_observability(settings: Settings) -> None:
    """Idempotently configure structlog + OpenTelemetry providers."""
    global _CONFIGURED
    if _CONFIGURED:
        return

    logging.basicConfig(
        level=getattr(logging, settings.log_level.upper(), logging.INFO),
        format="%(message)s",
    )

    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            _add_severity,
            _ensure_required_context,
            structlog.processors.TimeStamper(fmt="iso", key="timestamp", utc=True),
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(
            getattr(logging, settings.log_level.upper(), logging.INFO)
        ),
        logger_factory=structlog.PrintLoggerFactory(),
        cache_logger_on_first_use=True,
    )

    if settings.otel_enabled:
        resource = Resource.create(
            {
                "service.name": settings.service_name,
                "deployment.environment": settings.environment,
            }
        )
        tracer_provider = TracerProvider(resource=resource)
        tracer_provider.add_span_processor(
            BatchSpanProcessor(
                OTLPSpanExporter(endpoint=settings.otel_endpoint, insecure=True)
            )
        )
        trace.set_tracer_provider(tracer_provider)

        reader = PeriodicExportingMetricReader(
            OTLPMetricExporter(endpoint=settings.otel_endpoint, insecure=True)
        )
        metrics.set_meter_provider(
            MeterProvider(resource=resource, metric_readers=[reader])
        )

    _CONFIGURED = True


def get_logger(name: str = "orchestrator") -> structlog.stdlib.BoundLogger:
    return structlog.get_logger(name)


def get_tracer() -> trace.Tracer:
    return trace.get_tracer("apoe.agent_orchestrator")


def get_meter() -> metrics.Meter:
    return metrics.get_meter("apoe.agent_orchestrator")


def bind_incident(incident_id: str) -> None:
    """Bind the incident id into the logging context for this async task."""
    structlog.contextvars.bind_contextvars(incident_id=incident_id)


def bind_agent(agent_name: str) -> None:
    structlog.contextvars.bind_contextvars(agent_name=agent_name)


def clear_context() -> None:
    structlog.contextvars.clear_contextvars()
