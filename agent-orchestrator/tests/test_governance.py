"""Phase 3 governance tests: approval_required, approvals API, audit, auth, dry-run."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from agent_orchestrator.approvals import AlreadyResolved, ApprovalQueue
from agent_orchestrator.audit import audit_event
from agent_orchestrator.blackboard import Blackboard
from agent_orchestrator.config import Settings, get_settings
from agent_orchestrator.models import ActionType, ProposedAction
from agent_orchestrator.safety import (
    PolicyMatch,
    PolicyRule,
    SafetyPolicy,
    compile_policy,
)

TERMINATE = ProposedAction(
    action_type=ActionType.TERMINATE_BLOCKING_QUERIES,
    target="postgres",
    params={"pid": 42},
    rationale="kill blocker",
)


# ---------------------------------------------------------------------------
# Safety policy: approval_required effect
# ---------------------------------------------------------------------------
def test_approval_required_effect_compiles_and_evaluates():
    policy = compile_policy(
        SafetyPolicy(
            rules=[
                PolicyRule(
                    name="gate-terminate",
                    effect="approval_required",
                    match=PolicyMatch(
                        action_types=["terminate_blocking_queries"],
                        min_confidence=0.5,
                    ),
                )
            ]
        )
    )
    verdict = policy.evaluate(TERMINATE, confidence=0.9)
    assert verdict.allowed is False
    assert verdict.requires_approval is True
    assert verdict.policy == "gate-terminate"

    # Below the confidence floor the rule doesn't match -> default deny
    verdict_low = policy.evaluate(TERMINATE, confidence=0.3)
    assert verdict_low.requires_approval is False
    assert verdict_low.policy == "__default__"


def test_approval_required_as_default_effect():
    policy = compile_policy(SafetyPolicy(default_effect="approval_required"))
    verdict = policy.evaluate(TERMINATE, confidence=0.9)
    assert verdict.allowed is False and verdict.requires_approval is True


def test_invalid_effect_still_raises():
    with pytest.raises(ValueError):
        compile_policy(SafetyPolicy(rules=[PolicyRule(name="x", effect="maybe")]))


# ---------------------------------------------------------------------------
# Approval queue
# ---------------------------------------------------------------------------
def test_queue_lifecycle():
    q = ApprovalQueue()
    item = q.enqueue("inc-1", TERMINATE, 0.9)
    assert q.pending() == [item]
    resolved = q.resolve(item.approval_id, "approved")
    assert resolved.status == "approved"
    assert q.pending() == []
    with pytest.raises(AlreadyResolved):
        q.resolve(item.approval_id, "rejected")
    with pytest.raises(KeyError):
        q.get("apr-nope")


# ---------------------------------------------------------------------------
# Audit log
# ---------------------------------------------------------------------------
def test_audit_appends_jsonl(tmp_path):
    settings = Settings(audit_log_path=tmp_path / "audit.jsonl", dry_run=True)
    audit_event(settings, "proposed", incident_id="inc-1",
                action_type="noop", rationale="r", allowed=False)
    audit_event(settings, "executed", incident_id="inc-1",
                action_type="noop", actor="human", status="applied")

    lines = (tmp_path / "audit.jsonl").read_text().splitlines()
    assert len(lines) == 2
    first, second = (json.loads(line) for line in lines)
    assert first["event"] == "proposed"
    assert first["dry_run"] is True
    assert first["actor"] == "apoe"
    assert {"timestamp", "incident_id", "action_type", "rationale"} <= first.keys()
    assert second["actor"] == "human"


def test_audit_write_failure_never_raises(tmp_path):
    settings = Settings(audit_log_path=tmp_path)  # a directory -> OSError
    audit_event(settings, "proposed", incident_id="i", action_type="noop")


# ---------------------------------------------------------------------------
# Dry-run visibility
# ---------------------------------------------------------------------------
def test_dry_run_processor_stamps_every_record():
    from agent_orchestrator.observability import _dry_run_processor

    proc = _dry_run_processor(True)
    assert proc(None, "info", {"event": "x"})["dry_run"] is True
    # An explicit value is never overwritten
    assert proc(None, "info", {"dry_run": False})["dry_run"] is False


async def test_dry_run_blocks_execution(tmp_path):
    from agent_orchestrator.remediation import RemediationEngine
    from agent_orchestrator.models import RemediationStatus

    engine = RemediationEngine(
        Settings(dry_run=True, audit_log_path=tmp_path / "a.jsonl"),
        SimpleNamespace(),  # type: ignore[arg-type] - must never be touched
    )
    result = await engine.execute(TERMINATE, set())
    assert result.status is RemediationStatus.DRY_RUN


# ---------------------------------------------------------------------------
# Orchestrator queues instead of executing
# ---------------------------------------------------------------------------
async def test_orchestrator_queues_approval_required_action(tmp_path):
    from agent_orchestrator.investigator import LLMResponse
    from agent_orchestrator.models import IncidentState
    from agent_orchestrator.orchestrator import Orchestrator

    calls = []

    async def terminate_backend(pid):
        calls.append(pid)
        return True

    class LLM:
        async def complete(self, messages, max_tokens):
            return LLMResponse(
                text=json.dumps({
                    "action": "diagnose", "root_cause": "db_lock_contention",
                    "confidence": 0.95, "rationale": "blocker",
                    "proposed_actions": [{
                        "action_type": "terminate_blocking_queries",
                        "target": "postgres", "params": {"pid": 42}}],
                }),
                tokens=10,
            )

    policy = compile_policy(
        SafetyPolicy(rules=[PolicyRule(name="gate", effect="approval_required")])
    )
    queue = ApprovalQueue()
    orch = Orchestrator(
        Settings(investigator_timeout_s=5.0, audit_log_path=tmp_path / "audit.jsonl"),
        SimpleNamespace(postgres=SimpleNamespace(terminate_backend=terminate_backend)),  # type: ignore[arg-type]
        blackboard=Blackboard(),
        agents=[],
        policy=policy,
        llm=LLM(),
        approvals=queue,
    )
    session = await orch.handle_incident("test")

    assert session.state is IncidentState.ESCALATED
    assert calls == []  # nothing executed
    pending = queue.pending()
    assert len(pending) == 1
    assert pending[0].action.action_type is ActionType.TERMINATE_BLOCKING_QUERIES

    audit = [json.loads(x) for x in (tmp_path / "audit.jsonl").read_text().splitlines()]
    assert audit[0]["event"] == "proposed"
    assert audit[0]["requires_approval"] is True


# ---------------------------------------------------------------------------
# API: auth + approval endpoints (TestClient, no live infra)
# ---------------------------------------------------------------------------
@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("APOE_API_KEY", "test-key")
    monkeypatch.setenv("APOE_AUDIT_LOG_PATH", str(tmp_path / "audit.jsonl"))
    monkeypatch.setenv("APOE_KNOWLEDGE_DB_PATH", str(tmp_path / "k.db"))
    monkeypatch.setenv("APOE_LAB_SOURCE_PATH", str(tmp_path / "no-lab"))
    monkeypatch.setenv("APOE_OTEL_ENABLED", "false")
    get_settings.cache_clear()

    from fastapi.testclient import TestClient
    from agent_orchestrator.main import app

    with TestClient(app) as c:
        yield c
    get_settings.cache_clear()


AUTH = {"X-API-Key": "test-key"}


def test_health_open(client):
    assert client.get("/health").status_code == 200


def test_mutating_endpoints_401_without_or_wrong_key(client):
    assert client.post("/incidents", json={"trigger": "x"}).status_code == 401
    assert client.post(
        "/incidents", json={"trigger": "x"}, headers={"X-API-Key": "wrong"}
    ).status_code == 401
    assert client.post("/approvals/apr-x/approve").status_code == 401
    assert client.post(
        "/approvals/apr-x/reject", json={"reason": "r"}
    ).status_code == 401
    assert client.post("/simulate/db-lock").status_code == 401


def test_approval_flow_approve(client, tmp_path):
    app_state = client.app.state
    incident = app_state.orchestrator.blackboard.create("inc-api", "test")
    noop = ProposedAction(action_type=ActionType.NOOP, target="none")
    item = app_state.approvals.enqueue(incident.incident_id, noop, 0.9)

    listed = client.get("/approvals").json()
    assert [x["approval_id"] for x in listed] == [item.approval_id]

    resp = client.post(f"/approvals/{item.approval_id}/approve", headers=AUTH)
    assert resp.status_code == 200
    assert resp.json()["status"] == "applied"
    assert client.get("/approvals").json() == []
    # Result recorded on the incident session
    assert incident.results[0].status.value == "applied"

    # Re-approving is a conflict
    assert client.post(
        f"/approvals/{item.approval_id}/approve", headers=AUTH
    ).status_code == 409

    audit = [json.loads(x) for x in (tmp_path / "audit.jsonl").read_text().splitlines()]
    assert [a["event"] for a in audit] == ["approved", "executed"]
    assert audit[0]["actor"] == "human"


def test_approval_flow_reject_records_reason(client, tmp_path):
    app_state = client.app.state
    noop = ProposedAction(action_type=ActionType.NOOP, target="none")
    item = app_state.approvals.enqueue("inc-r", noop, 0.5)

    resp = client.post(
        f"/approvals/{item.approval_id}/reject",
        json={"reason": "not during trading hours", "actor": "sre-jane"},
        headers=AUTH,
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "rejected"
    assert body["reason"] == "not during trading hours"

    audit = [json.loads(x) for x in (tmp_path / "audit.jsonl").read_text().splitlines()]
    assert audit[-1]["event"] == "rejected"
    assert audit[-1]["actor"] == "sre-jane"
    assert audit[-1]["rationale"] == "not during trading hours"


def test_change_webhook_records_and_lists(client):
    resp = client.post(
        "/changes",
        json={"service": "order-service", "change_kind": "deploy",
              "summary": "release 3.0", "actor": "cd"},
        headers=AUTH,
    )
    assert resp.status_code == 200
    assert resp.json()["change_id"].startswith("chg-")

    listed = client.get("/changes").json()
    assert listed[0]["summary"] == "release 3.0"
    assert client.get("/changes", params={"service": "nope"}).json() == []

    # Auth required to record
    assert client.post(
        "/changes", json={"service": "x", "summary": "y"}
    ).status_code == 401


def test_approval_unknown_id_404(client):
    assert client.post("/approvals/apr-nope/approve", headers=AUTH).status_code == 404
    assert client.post(
        "/approvals/apr-nope/reject", json={"reason": "r"}, headers=AUTH
    ).status_code == 404
