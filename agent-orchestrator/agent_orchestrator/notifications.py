"""Notification dispatch: Slack webhook + generic webhook.

Fires on incident state transitions (ESCALATED, RESOLVED, FAILED).
Configured via APOE_SLACK_WEBHOOK_URL and/or APOE_NOTIFY_WEBHOOK_URL.
Empty URL = silently skipped (no dependency on external services).
"""

from __future__ import annotations

from typing import Any

import httpx

from .models import IncidentSession
from .observability import get_logger

log = get_logger("notifications")

_INCIDENT_EMOJI = {
    "escalated": ":rotating_light:",
    "resolved": ":white_check_mark:",
    "failed": ":x:",
}


def _slack_payload(session: IncidentSession) -> dict[str, Any]:
    emoji = _INCIDENT_EMOJI.get(session.state.value, ":bell:")
    diag = session.diagnosis
    root = diag.root_cause.value if diag else "unknown"
    confidence = f"{diag.confidence:.0%}" if diag else "—"
    blocks = [
        {
            "type": "header",
            "text": {"type": "plain_text",
                     "text": f"{emoji} Incident {session.state.value.upper()}: {session.incident_id}"},
        },
        {
            "type": "section",
            "fields": [
                {"type": "mrkdwn", "text": f"*Root cause:* `{root}`"},
                {"type": "mrkdwn", "text": f"*Confidence:* {confidence}"},
                {"type": "mrkdwn", "text": f"*Trigger:* {session.trigger}"},
                {"type": "mrkdwn", "text": f"*Actions:* {len(session.results)}"},
            ],
        },
    ]
    if diag and diag.rationale:
        blocks.append({
            "type": "section",
            "text": {"type": "mrkdwn", "text": f"*Rationale:* {diag.rationale[:500]}"},
        })
    return {"blocks": blocks}


def _generic_payload(session: IncidentSession) -> dict[str, Any]:
    return session.model_dump(mode="json")


async def notify(
    session: IncidentSession,
    slack_url: str = "",
    webhook_url: str = "",
) -> None:
    """Post incident summary to configured endpoints. Failures log, never raise."""
    async with httpx.AsyncClient(timeout=10.0) as client:
        if slack_url:
            try:
                resp = await client.post(slack_url, json=_slack_payload(session))
                resp.raise_for_status()
                log.info("slack_notified", incident_id=session.incident_id)
            except Exception as exc:
                log.warning("slack_notify_failed", error=str(exc),
                            incident_id=session.incident_id)
        if webhook_url:
            try:
                resp = await client.post(webhook_url, json=_generic_payload(session))
                resp.raise_for_status()
                log.info("webhook_notified", incident_id=session.incident_id)
            except Exception as exc:
                log.warning("webhook_notify_failed", error=str(exc),
                            incident_id=session.incident_id)
