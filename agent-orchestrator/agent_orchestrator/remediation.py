"""Idempotent remediation engine.

Every action is replay-safe on two levels:

1. **Session-level guard.** Before executing, the engine checks the action's
   ``idempotency_key`` against keys already applied in this incident session
   (recorded on the blackboard). A replay is a no-op → ``SKIPPED_REPLAY``.
2. **Action-level idempotency.** The underlying operations are themselves
   idempotent: ``pg_terminate_backend`` on a gone pid returns false without
   error, ``reset`` on the chaos sandbox is a pure clear, ``DEL`` on a missing
   key returns 0.

The engine never *decides* whether an action is permitted — that is the safety
compiler's job. It only executes actions that arrive already-approved.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from .config import Settings
from .connectors import Connectors
from .models import ActionType, ProposedAction, RemediationResult, RemediationStatus
from .observability import get_logger

log = get_logger("remediation")

Handler = Callable[[ProposedAction, Connectors], Awaitable[str]]


async def _terminate_blocking_queries(
    action: ProposedAction, connectors: Connectors
) -> str:
    pid = int(action.params["pid"])
    terminated = await connectors.postgres.terminate_backend(pid)
    return (
        f"terminated backend pid={pid}"
        if terminated
        else f"backend pid={pid} already gone (idempotent no-op)"
    )


async def _reset_chaos_sandbox(
    action: ProposedAction, connectors: Connectors
) -> str:
    result = await connectors.chaos.reset()
    return f"chaos sandbox reset: {result.get('scenario', 'ok')}"


async def _flush_cache_key(action: ProposedAction, connectors: Connectors) -> str:
    key = str(action.params["key"])
    deleted = await connectors.redis.delete_key(key)
    return f"flushed cache key '{key}' (deleted={deleted})"


async def _noop(action: ProposedAction, connectors: Connectors) -> str:
    return "noop"


_HANDLERS: dict[ActionType, Handler] = {
    ActionType.TERMINATE_BLOCKING_QUERIES: _terminate_blocking_queries,
    ActionType.RESET_CHAOS_SANDBOX: _reset_chaos_sandbox,
    ActionType.FLUSH_CACHE_KEY: _flush_cache_key,
    ActionType.NOOP: _noop,
}


class RemediationEngine:
    def __init__(self, settings: Settings, connectors: Connectors) -> None:
        self._settings = settings
        self._connectors = connectors

    async def execute(
        self, action: ProposedAction, already_applied: set[str]
    ) -> RemediationResult:
        """Execute one approved action, honouring idempotency and dry-run."""
        key = action.idempotency_key
        if key in already_applied:
            log.info("remediation_replay_skipped", idempotency_key=key)
            return RemediationResult(
                action=action,
                status=RemediationStatus.SKIPPED_REPLAY,
                detail="idempotency key already applied in this session",
            )

        if self._settings.dry_run:
            log.info("remediation_dry_run", action=action.action_type.value)
            return RemediationResult(
                action=action,
                status=RemediationStatus.DRY_RUN,
                detail="dry_run enabled; action not executed",
            )

        handler = _HANDLERS.get(action.action_type)
        if handler is None:
            return RemediationResult(
                action=action,
                status=RemediationStatus.FAILED,
                detail=f"no handler for {action.action_type.value}",
            )

        try:
            detail = await handler(action, self._connectors)
            log.info(
                "remediation_applied",
                action=action.action_type.value,
                target=action.target,
                detail=detail,
            )
            return RemediationResult(
                action=action, status=RemediationStatus.APPLIED, detail=detail
            )
        except Exception as exc:
            log.error(
                "remediation_failed",
                action=action.action_type.value,
                error=str(exc),
                error_type=type(exc).__name__,
            )
            return RemediationResult(
                action=action,
                status=RemediationStatus.FAILED,
                detail=f"{type(exc).__name__}: {exc}",
            )
