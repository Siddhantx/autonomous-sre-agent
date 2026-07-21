"""Human-approval queue for actions gated by ``effect: approval_required``.

The orchestrator enqueues; the ``/approvals`` API resolves. Approved actions
execute through the normal idempotent remediation engine — the queue never
runs anything itself.

# ponytail: in-memory dict; pending approvals do not survive a restart. The
# JSONL audit log is the durable record. Move to a sqlite table if operators
# need restart-safe queues.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, Field

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
