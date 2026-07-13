"""Async, retrying connectors to the enterprise-lab data plane.

Every external call is wrapped in a ``tenacity`` exponential-backoff retry.
Connectors *raise* on definitive failure; the diagnostic agents (not the
connectors) decide to degrade. This keeps the "failed scrape must not crash
the orchestrator" contract in one place — the agent layer — while the
connector layer stays a thin, retrying transport.

Connections are lazy and cached per connector instance. ``aclose`` releases
them; the orchestrator closes all connectors at shutdown.
"""

from __future__ import annotations

from typing import Any

import asyncpg
import httpx
import redis.asyncio as aioredis
from tenacity import (
    AsyncRetrying,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from .config import Settings
from .observability import get_logger

log = get_logger("connector")


def _retryer(settings: Settings, *exc: type[BaseException]) -> AsyncRetrying:
    """Build a fresh tenacity controller from settings (retries are stateful)."""
    return AsyncRetrying(
        stop=stop_after_attempt(settings.max_retry_attempts),
        wait=wait_exponential(
            multiplier=settings.retry_backoff_seconds,
            max=settings.retry_backoff_max_seconds,
        ),
        retry=retry_if_exception_type(exc or (Exception,)),
        reraise=True,
    )


class PrometheusConnector:
    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._client = httpx.AsyncClient(base_url=settings.prometheus_url, timeout=5.0)

    async def instant_query(self, promql: str) -> float | None:
        """Return the scalar value of an instant PromQL query, or None if empty."""
        async for attempt in _retryer(self._settings, httpx.HTTPError):
            with attempt:
                resp = await self._client.get(
                    "/api/v1/query", params={"query": promql}
                )
                resp.raise_for_status()
                body = resp.json()
                results = body.get("data", {}).get("result", [])
                if not results:
                    return None
                return float(results[0]["value"][1])
        return None  # pragma: no cover - reraise=True makes this unreachable

    async def aclose(self) -> None:
        await self._client.aclose()


class PostgresConnector:
    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._pool: asyncpg.Pool | None = None

    async def _ensure_pool(self) -> asyncpg.Pool:
        if self._pool is None:
            async for attempt in _retryer(self._settings):
                with attempt:
                    self._pool = await asyncpg.create_pool(
                        dsn=self._settings.postgres_dsn,
                        min_size=1,
                        max_size=4,
                        command_timeout=5.0,
                    )
        assert self._pool is not None
        return self._pool

    async def blocking_backends(self) -> list[dict[str, Any]]:
        """Return backends that are *blocking* others, via ``pg_blocking_pids``.

        This is the concrete, always-available signal for the DB-lock chaos
        scenario. Each row is one blocked victim and its blocker.
        """
        pool = await self._ensure_pool()
        rows = await pool.fetch(
            """
            SELECT blocked.pid          AS blocked_pid,
                   blocker.pid          AS blocking_pid,
                   blocker.query        AS blocking_query,
                   blocker.state        AS blocking_state
            FROM pg_stat_activity AS blocked
            JOIN LATERAL unnest(pg_blocking_pids(blocked.pid)) AS bp(pid) ON TRUE
            JOIN pg_stat_activity AS blocker ON blocker.pid = bp.pid
            WHERE cardinality(pg_blocking_pids(blocked.pid)) > 0
            """
        )
        return [dict(r) for r in rows]

    async def terminate_backend(self, pid: int) -> bool:
        """Terminate a backend by pid. Idempotent: a gone pid returns False.

        ``pg_terminate_backend`` returns false (and does not error) if the pid
        no longer exists, so replaying this action is always safe.
        """
        pool = await self._ensure_pool()
        return bool(await pool.fetchval("SELECT pg_terminate_backend($1)", pid))

    async def aclose(self) -> None:
        if self._pool is not None:
            await self._pool.close()


class RedisConnector:
    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._client = aioredis.from_url(
            settings.redis_url, socket_timeout=5.0, decode_responses=True
        )

    async def ping_latency_ms(self) -> float:
        import time

        async for attempt in _retryer(self._settings):
            with attempt:
                start = time.perf_counter()
                await self._client.ping()
                return (time.perf_counter() - start) * 1000.0
        return -1.0  # pragma: no cover

    async def delete_key(self, key: str) -> int:
        """Delete a cache key. Idempotent: deleting a missing key returns 0."""
        return int(await self._client.delete(key))

    async def aclose(self) -> None:
        await self._client.aclose()


class KafkaConnector:
    """Best-effort consumer-lag probe via aiokafka's admin/consumer APIs."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    async def total_consumer_lag(self, group_id: str, topic: str) -> int:
        # Imported lazily so the rest of the orchestrator does not hard-depend
        # on aiokafka being importable in every context (e.g. unit tests).
        from aiokafka import AIOKafkaConsumer
        from aiokafka.structs import TopicPartition

        consumer = AIOKafkaConsumer(
            bootstrap_servers=self._settings.kafka_bootstrap,
            group_id=group_id,
            enable_auto_commit=False,
        )
        await consumer.start()
        try:
            partitions = consumer.partitions_for_topic(topic) or set()
            tps = [TopicPartition(topic, p) for p in partitions]
            if not tps:
                return 0
            end_offsets = await consumer.end_offsets(tps)
            committed = {tp: await consumer.committed(tp) for tp in tps}
            return sum(
                end_offsets[tp] - (committed[tp] or 0) for tp in tps
            )
        finally:
            await consumer.stop()


class ChaosConnector:
    """Client for the chaos-injector — used to drive the e2e incident sim."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._client = httpx.AsyncClient(
            base_url=settings.chaos_injector_url, timeout=10.0
        )

    async def trigger(self, scenario: str) -> dict[str, Any]:
        resp = await self._client.post(f"/chaos/{scenario}")
        resp.raise_for_status()
        return resp.json()

    async def reset(self) -> dict[str, Any]:
        """Reset the sandbox. Idempotent by construction on the injector side."""
        resp = await self._client.post("/chaos/reset")
        resp.raise_for_status()
        return resp.json()

    async def aclose(self) -> None:
        await self._client.aclose()


class Connectors:
    """Bundle of all connectors, constructed once and shared."""

    def __init__(self, settings: Settings) -> None:
        self.prometheus = PrometheusConnector(settings)
        self.postgres = PostgresConnector(settings)
        self.redis = RedisConnector(settings)
        self.kafka = KafkaConnector(settings)
        self.chaos = ChaosConnector(settings)

    async def aclose(self) -> None:
        await self.prometheus.aclose()
        await self.postgres.aclose()
        await self.redis.aclose()
        await self.chaos.aclose()
