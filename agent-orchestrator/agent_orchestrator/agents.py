"""Diagnostic agents.

Each agent observes exactly one subsystem and returns a :class:`Finding`. The
base class enforces two of the strict engineering requirements:

* **Per-agent timeout** — every ``observe`` is wrapped in ``asyncio.wait_for``
  with a hard ceiling (default 10s, from settings).
* **Graceful degradation** — a timeout or connector failure never propagates;
  it becomes a ``degraded`` finding (``status=UNAVAILABLE``) so a broken
  Prometheus scrape marks its data unavailable instead of crashing the run.

The orchestrator fans these out with ``asyncio.gather``.
"""

from __future__ import annotations

import abc
import asyncio

from opentelemetry import trace

from .config import Settings
from .connectors import Connectors
from .models import Finding, Severity, SubsystemStatus
from .observability import bind_agent, get_logger, get_tracer

log = get_logger("agent")


class DiagnosticAgent(abc.ABC):
    """Base diagnostic agent. Subclasses implement :meth:`_observe`."""

    name: str
    subsystem: str

    async def _observe(self, connectors: Connectors) -> Finding:  # pragma: no cover
        raise NotImplementedError

    async def run(self, connectors: Connectors, timeout: float) -> Finding:
        """Run the agent under a timeout, degrading on any failure."""
        bind_agent(self.name)
        tracer = get_tracer()
        with tracer.start_as_current_span(f"agent.{self.name}") as span:
            try:
                finding = await asyncio.wait_for(
                    self._observe(connectors), timeout=timeout
                )
                span.set_attribute("agent.status", finding.status.value)
                span.set_attribute("agent.degraded", finding.degraded)
                return finding
            except asyncio.TimeoutError:
                log.warning("agent_timeout", timeout=timeout)
                span.set_status(trace.Status(trace.StatusCode.ERROR, "timeout"))
                return self._degraded(f"timed out after {timeout:.0f}s")
            except Exception as exc:  # graceful degradation, never crash the run
                log.warning("agent_error", error=str(exc), error_type=type(exc).__name__)
                span.record_exception(exc)
                return self._degraded(f"{type(exc).__name__}: {exc}")

    def _degraded(self, reason: str) -> Finding:
        return Finding(
            agent_name=self.name,
            subsystem=self.subsystem,
            status=SubsystemStatus.UNAVAILABLE,
            severity=Severity.WARNING,
            summary=f"{self.subsystem} signal unavailable ({reason})",
            degraded=True,
        )


class DbLockAgent(DiagnosticAgent):
    """Detects lock contention via ``pg_blocking_pids`` — the reliable signal."""

    name = "db-lock-agent"
    subsystem = "postgres"

    async def _observe(self, connectors: Connectors) -> Finding:
        blockers = await connectors.postgres.blocking_backends()
        if not blockers:
            return Finding(
                agent_name=self.name,
                subsystem=self.subsystem,
                status=SubsystemStatus.HEALTHY,
                severity=Severity.INFO,
                summary="no blocking backends detected",
                metrics={"blocking_backends": 0.0},
            )
        blocking_pids = sorted({b["blocking_pid"] for b in blockers})
        return Finding(
            agent_name=self.name,
            subsystem=self.subsystem,
            status=SubsystemStatus.FAULTED,
            severity=Severity.CRITICAL,
            summary=(
                f"{len(blockers)} backend(s) blocked by "
                f"{len(blocking_pids)} blocker(s): pids {blocking_pids}"
            ),
            metrics={
                "blocking_backends": float(len(blocking_pids)),
                "blocked_backends": float(len(blockers)),
                # primary blocker pid surfaced for the remediation target
                "primary_blocking_pid": float(blocking_pids[0]),
            },
        )


class CpuAgent(DiagnosticAgent):
    """CPU saturation via Prometheus. Degrades if the scrape has no data."""

    name = "cpu-agent"
    subsystem = "compute"
    _SATURATION = 0.85  # ratio of a core

    async def _observe(self, connectors: Connectors) -> Finding:
        value = await connectors.prometheus.instant_query(
            "max(rate(process_cpu_seconds_total[1m]))"
        )
        if value is None:
            return self._degraded("prometheus returned no series")
        faulted = value >= self._SATURATION
        return Finding(
            agent_name=self.name,
            subsystem=self.subsystem,
            status=SubsystemStatus.FAULTED if faulted else SubsystemStatus.HEALTHY,
            severity=Severity.CRITICAL if faulted else Severity.INFO,
            summary=f"peak cpu utilisation {value:.2f} cores",
            metrics={"cpu_ratio": value},
        )


class MemoryAgent(DiagnosticAgent):
    """Memory-leak heuristic via Prometheus resident memory."""

    name = "memory-agent"
    subsystem = "memory"
    _LEAK_BYTES = 400 * 1024 * 1024  # 400 MiB

    async def _observe(self, connectors: Connectors) -> Finding:
        value = await connectors.prometheus.instant_query(
            "max(process_resident_memory_bytes)"
        )
        if value is None:
            return self._degraded("prometheus returned no series")
        faulted = value >= self._LEAK_BYTES
        return Finding(
            agent_name=self.name,
            subsystem=self.subsystem,
            status=SubsystemStatus.FAULTED if faulted else SubsystemStatus.HEALTHY,
            severity=Severity.CRITICAL if faulted else Severity.INFO,
            summary=f"peak resident memory {value / 1024 / 1024:.0f} MiB",
            metrics={"resident_bytes": value},
        )


class CacheAgent(DiagnosticAgent):
    """Redis availability / latency probe."""

    name = "cache-agent"
    subsystem = "redis"
    _SLOW_MS = 250.0

    async def _observe(self, connectors: Connectors) -> Finding:
        latency = await connectors.redis.ping_latency_ms()
        faulted = latency >= self._SLOW_MS
        return Finding(
            agent_name=self.name,
            subsystem=self.subsystem,
            status=SubsystemStatus.FAULTED if faulted else SubsystemStatus.HEALTHY,
            severity=Severity.WARNING if faulted else Severity.INFO,
            summary=f"redis ping {latency:.1f} ms",
            metrics={"ping_ms": latency},
        )


class KafkaLagAgent(DiagnosticAgent):
    """Consumer-lag probe for the payment pipeline."""

    name = "kafka-lag-agent"
    subsystem = "kafka"
    _LAG_THRESHOLD = 1000.0

    async def _observe(self, connectors: Connectors) -> Finding:
        lag = await connectors.kafka.total_consumer_lag(
            group_id="payment-processors", topic="order-events"
        )
        faulted = lag >= self._LAG_THRESHOLD
        return Finding(
            agent_name=self.name,
            subsystem=self.subsystem,
            status=SubsystemStatus.FAULTED if faulted else SubsystemStatus.HEALTHY,
            severity=Severity.WARNING if faulted else Severity.INFO,
            summary=f"payment-processors lag {lag} messages",
            metrics={"consumer_lag": float(lag)},
        )


def default_agents() -> list[DiagnosticAgent]:
    return [
        DbLockAgent(),
        CpuAgent(),
        MemoryAgent(),
        CacheAgent(),
        KafkaLagAgent(),
    ]


async def run_all(
    agents: list[DiagnosticAgent], connectors: Connectors, settings: Settings
) -> list[Finding]:
    """Fan out all agents concurrently with per-agent timeouts."""
    return await asyncio.gather(
        *(a.run(connectors, settings.agent_timeout_seconds) for a in agents)
    )
