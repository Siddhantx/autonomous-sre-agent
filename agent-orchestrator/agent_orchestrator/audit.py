"""Append-only JSONL audit log.

Every proposed, approved, rejected and executed action becomes one line with
actor, timestamp, incident_id, action_type, rationale and the dry_run flag.
Path from ``APOE_AUDIT_LOG_PATH``; append mode only — never truncated.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

from .config import Settings
from .observability import get_logger

log = get_logger("audit")


def audit_event(
    settings: Settings,
    event: str,
    *,
    incident_id: str,
    action_type: str,
    rationale: str = "",
    actor: str = "apoe",
    **extra: object,
) -> None:
    """Append one audit record. Failures are logged, never raised.

    # ponytail: audit failure does not block the action; flip to fail-closed
    # (refuse to act when the audit write fails) if compliance demands it.
    """
    record = {
        "timestamp": datetime.now(tz=timezone.utc).isoformat(),
        "event": event,
        "actor": actor,
        "incident_id": incident_id,
        "action_type": action_type,
        "rationale": rationale,
        "dry_run": settings.dry_run,
        **extra,
    }
    try:
        with open(settings.audit_log_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, default=str) + "\n")
    except Exception as exc:  # never let an audit failure block the pipeline
        log.error("audit_write_failed", error=str(exc), audit_event=event)
