"""Human-approval queue for actions gated by ``effect: approval_required``.

The orchestrator enqueues; the ``/approvals`` API resolves. Approved actions
execute through the normal idempotent remediation engine — the queue never
runs anything itself.

# ponytail: in-memory dict; pending approvals do not survive a restart. The
# JSONL audit log is the durable record. Move to a sqlite table if operators
# need restart-safe queues.
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field

from .config import Settings
from .models import ProposedAction, _utcnow


class PendingApproval(BaseModel):
    approval_id: str
    incident_id: str
    action: ProposedAction
    confidence: float
    status: str = "pending"  # pending | approved | rejected
    reason: str = ""
    created_at: datetime = Field(default_factory=_utcnow)
    resolved_at: datetime | None = None


class AlreadyResolved(RuntimeError):
    pass


class ApprovalQueue:
    def __init__(self) -> None:
        self._items: dict[str, PendingApproval] = {}

    def enqueue(
        self, incident_id: str, action: ProposedAction, confidence: float
    ) -> PendingApproval:
        item = PendingApproval(
            approval_id=f"apr-{uuid.uuid4().hex[:10]}",
            incident_id=incident_id,
            action=action,
            confidence=confidence,
        )
        self._items[item.approval_id] = item
        return item

    def get(self, approval_id: str) -> PendingApproval:
        return self._items[approval_id]  # KeyError -> API 404

    def pending(self) -> list[PendingApproval]:
        return [i for i in self._items.values() if i.status == "pending"]

    def resolve(
        self, approval_id: str, status: str, reason: str = ""
    ) -> PendingApproval:
        item = self.get(approval_id)
        if item.status != "pending":
            raise AlreadyResolved(f"{approval_id} already {item.status}")
        item.status = status
        item.reason = reason
        item.resolved_at = _utcnow()
        return item


def promotion_candidates(settings: Settings) -> list[dict[str, Any]]:
    """Actions humans keep approving — candidates for an auto-allow rule.

    Derived from the append-only audit log (restart-safe by construction):
    an (action_type, target) pair with >= APOE_PROMOTION_THRESHOLD
    *consecutive* approvals and no later rejection becomes a candidate.
    Nothing is auto-applied — the output is a ready-to-review YAML rule a
    human merges into policies.yaml.
    """
    streaks: dict[tuple[str, str], int] = {}
    try:
        lines = settings.audit_log_path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    for line in lines:
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        if record.get("event") not in ("approved", "rejected"):
            continue
        key = (str(record.get("action_type")), str(record.get("target", "*")))
        if record["event"] == "approved":
            streaks[key] = streaks.get(key, 0) + 1
        else:
            streaks[key] = 0  # a rejection resets the streak

    candidates = []
    for (action_type, target), streak in sorted(streaks.items()):
        if streak >= settings.promotion_threshold:
            candidates.append(
                {
                    "action_type": action_type,
                    "target": target,
                    "consecutive_approvals": streak,
                    "suggested_rule": (
                        f"- name: allow-{action_type.replace('_', '-')}\n"
                        f"  effect: allow\n"
                        f"  match:\n"
                        f"    action_types: [\"{action_type}\"]\n"
                        f"    targets: [\"{target}\"]\n"
                        f"    min_confidence: 0.8\n"
                    ),
                }
            )
    return candidates
