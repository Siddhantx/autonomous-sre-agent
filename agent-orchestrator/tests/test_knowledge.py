"""Knowledge layer tests: store, ingestion, retrieval, learning loop."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

from agent_orchestrator.blackboard import Blackboard
from agent_orchestrator.config import Settings
from agent_orchestrator.knowledge import (
    KnowledgeStore,
    ingest_all,
    ingest_git_history,
    ingest_lab_sources,
    ingest_runbooks,
)
from agent_orchestrator.models import (
    ActionType,
    Diagnosis,
    Finding,
    ProposedAction,
    RemediationResult,
    RemediationStatus,
    RootCause,
    Severity,
    SubsystemStatus,
)

RUNBOOKS_DIR = Path(__file__).resolve().parents[1] / "runbooks"


# ---------------------------------------------------------------------------
# Store: add / upsert / search
# ---------------------------------------------------------------------------
def test_add_and_search_ranked():
    store = KnowledgeStore()
    store.add("runbook", "a.md", "Postgres locks", "terminate the blocking backend")
    store.add("code", "svc/app.py", "app.py", "def create_order(): pass")
    hits = store.search("blocking backend")
    assert hits and hits[0].ref == "a.md"
    assert hits[0].kind == "runbook"


def test_upsert_no_duplicates():
    store = KnowledgeStore()
    store.add("runbook", "a.md", "v1", "old content lockstorm")
    store.add("runbook", "a.md", "v2", "new content lockstorm")
    hits = store.search("lockstorm")
    assert len(hits) == 1
    assert hits[0].title == "v2"


def test_kind_filter():
    store = KnowledgeStore()
    store.add("runbook", "a.md", "locks", "postgres deadlock")
    store.add("incident", "inc-1", "locks", "postgres deadlock resolved")
    assert {h.kind for h in store.search("deadlock")} == {"runbook", "incident"}
    assert [h.kind for h in store.search("deadlock", kind="incident")] == ["incident"]


def test_search_edge_cases():
    store = KnowledgeStore()
    store.add("runbook", "a.md", "t", "content here")
    assert store.search("") == []
    assert store.search('   "  ') == []
    # FTS metacharacters must not raise
    assert store.search('content AND "quoted*" (x)') is not None


# ---------------------------------------------------------------------------
# Ingestion
# ---------------------------------------------------------------------------
def test_ingest_lab_sources_code_and_topology(tmp_path):
    (tmp_path / "order-service").mkdir()
    (tmp_path / "order-service" / "app.py").write_text("MAX_POOL = 10")
    (tmp_path / "docker-compose.yml").write_text("services:\n  postgres:\n    image: postgres")
    (tmp_path / "ignore.bin").write_bytes(b"\x00")

    store = KnowledgeStore()
    assert ingest_lab_sources(store, tmp_path) == 2
    assert store.search("MAX_POOL", kind="code")[0].ref.endswith("app.py")
    assert store.search("postgres image", kind="topology")

    # Idempotent re-ingest
    assert ingest_lab_sources(store, tmp_path) == 2
    assert len(store.search("MAX_POOL")) == 1


def test_ingest_missing_dirs_return_zero(tmp_path):
    store = KnowledgeStore()
    assert ingest_lab_sources(store, tmp_path / "nope") == 0
    assert ingest_runbooks(store, tmp_path / "nope") == 0


def test_ingest_real_runbooks():
    store = KnowledgeStore()
    assert ingest_runbooks(store, RUNBOOKS_DIR) == 5
    hits = store.search("blocking pid terminate", kind="runbook")
    assert hits and "lock" in hits[0].title.lower()
    # Titles come from the first markdown heading
    assert all(not h.title.startswith("#") for h in store.search("the", kind="runbook"))


def test_ingest_all_uses_settings(tmp_path):
    (tmp_path / "lab").mkdir()
    (tmp_path / "lab" / "svc.py").write_text("x = 1")
    settings = Settings(lab_source_path=tmp_path / "lab", runbooks_path=RUNBOOKS_DIR)
    store = KnowledgeStore()
    counts = ingest_all(store, settings)
    assert counts["lab_documents"] == 1
    assert counts["runbooks"] == 5
    assert counts["git_commits"] == 0  # tmp dir is not a git repo


# ---------------------------------------------------------------------------
# Change events ("what changed?")
# ---------------------------------------------------------------------------
def test_record_and_list_changes_newest_first():
    store = KnowledgeStore()
    store.record_change("order-service", "deploy", "release 1.0", actor="cd")
    store.record_change("postgres", "schema", "migration 0042", actor="dba")
    changes = store.recent_changes(10)
    assert [c["service"] for c in changes] == ["postgres", "order-service"]
    assert changes[0]["change_kind"] == "schema"
    assert changes[0]["actor"] == "dba"
    # Service filter
    assert [c["service"] for c in store.recent_changes(10, service="order-service")] \
        == ["order-service"]
    # Changes are full-text searchable too
    assert store.search("migration 0042", kind="change")


def test_ingest_git_history_real_repo_and_missing_dir(tmp_path):
    store = KnowledgeStore()
    repo_root = Path(__file__).resolve().parents[1]
    n = ingest_git_history(store, repo_root, n=5)
    if n:  # git + repo available (dev machine, CI)
        changes = store.recent_changes(5)
        assert changes and changes[0]["change_kind"] == "commit"
        assert changes[0]["service"] == "codebase"
    assert ingest_git_history(store, tmp_path / "not-a-repo") == 0


async def test_recent_changes_tool_and_prompt_injection():
    from agent_orchestrator.investigator import (
        LLMResponse,
        ToolContext,
        _dispatch_tool,
        investigate,
    )
    from types import SimpleNamespace

    store = KnowledgeStore()
    store.record_change("order-service", "deploy", "release 2.14.1 broke config",
                        actor="cd-pipeline")

    bb = Blackboard()
    session = bb.create("inc-chg", "test")
    ctx = ToolContext(SimpleNamespace(), session, bb, Settings(), store)
    payload = await _dispatch_tool("recent_changes", {"service": "order-service"}, ctx)
    assert "release 2.14.1" in payload
    ctx_none = ToolContext(SimpleNamespace(), session, bb, Settings(), None)
    assert "not configured" in await _dispatch_tool("recent_changes", {}, ctx_none)

    class OneShot:
        def __init__(self):
            self.calls = []

        async def complete(self, messages, max_tokens):
            self.calls.append(messages)
            return LLMResponse(
                text='{"action": "diagnose", "root_cause": "bad_config_deploy", '
                     '"confidence": 0.8, "rationale": "deploy"}',
                tokens=10,
            )

    llm = OneShot()
    await investigate(session, bb, SimpleNamespace(),
                      Settings(investigator_timeout_s=5.0), llm, store)
    first_msg = llm.calls[0][1]["content"]
    assert "Recent changes (newest first):" in first_msg
    assert "release 2.14.1" in first_msg


# ---------------------------------------------------------------------------
# Learning loop: post-mortems
# ---------------------------------------------------------------------------
def make_closed_session():
    bb = Blackboard()
    session = bb.create("inc-pm-1", "chaos:db-lock")
    session.findings.append(
        Finding(
            agent_name="db-lock-agent", subsystem="postgres",
            status=SubsystemStatus.FAULTED, severity=Severity.CRITICAL,
            summary="pid 42 blocking",
        )
    )
    action = ProposedAction(
        action_type=ActionType.TERMINATE_BLOCKING_QUERIES,
        target="postgres", params={"pid": 42},
    )
    session.diagnosis = Diagnosis(
        root_cause=RootCause.DB_LOCK_CONTENTION, confidence=0.95,
        rationale="lead blocker must die", evidence=["[pg_blocking] pid 42"],
        proposed_actions=[action],
    )
    session.results.append(
        RemediationResult(
            action=action, status=RemediationStatus.APPLIED,
            detail="terminated backend pid=42",
        )
    )
    from agent_orchestrator.models import IncidentState
    session.state = IncidentState.RESOLVED
    return session


def test_post_mortem_recorded_and_searchable():
    store = KnowledgeStore()
    store.add_post_mortem(make_closed_session())

    hits = store.search("db_lock_contention resolved", kind="incident")
    assert hits and hits[0].ref == "inc-pm-1"

    # The stored record carries the full structured JSON
    row = store._conn.execute(
        "SELECT content FROM knowledge WHERE kind='incident'"
    ).fetchone()
    record = json.loads(row[0].splitlines()[-1])
    assert record["final_state"] == "resolved"
    assert record["actions"][0]["action"] == "terminate_blocking_queries"
    assert record["actions"][0]["status"] == "applied"


def test_post_mortem_without_diagnosis():
    bb = Blackboard()
    session = bb.create("inc-pm-2", "manual")
    store = KnowledgeStore()
    store.add_post_mortem(session)  # must not raise
    row = store._conn.execute(
        "SELECT title FROM knowledge WHERE ref='inc-pm-2'"
    ).fetchone()
    assert row[0].startswith("unknown")


def test_post_mortem_upserts_on_same_incident():
    store = KnowledgeStore()
    session = make_closed_session()
    store.add_post_mortem(session)
    store.add_post_mortem(session)
    rows = store._conn.execute(
        "SELECT count(*) FROM knowledge WHERE kind='incident'"
    ).fetchone()
    assert rows[0] == 1


# ---------------------------------------------------------------------------
# Investigator integration: knowledge consulted first + tool
# ---------------------------------------------------------------------------
async def test_investigator_seeds_knowledge_before_live_infra():
    from agent_orchestrator.investigator import LLMResponse, investigate

    store = KnowledgeStore()
    store.add("runbook", "db-lock.md", "Postgres lock contention",
              "terminate the lead blocking backend")

    class OneShotLLM:
        def __init__(self):
            self.calls = []

        async def complete(self, messages, max_tokens):
            self.calls.append(messages)
            return LLMResponse(
                text=json.dumps({"action": "diagnose", "root_cause": "db_lock_contention",
                                 "confidence": 0.9, "rationale": "runbook match"}),
                tokens=50,
            )

    bb = Blackboard()
    session = bb.create("inc-k", "test")
    session.findings.append(
        Finding(agent_name="db-lock-agent", subsystem="postgres",
                status=SubsystemStatus.FAULTED, severity=Severity.CRITICAL,
                summary="backend blocking others")
    )
    llm = OneShotLLM()
    d = await investigate(
        session, bb, SimpleNamespace(), Settings(investigator_timeout_s=5.0),
        llm, store,
    )
    assert d.root_cause is RootCause.DB_LOCK_CONTENTION
    first_user_msg = llm.calls[0][1]["content"]
    assert "Knowledge base (pre-searched)" in first_user_msg
    assert "Postgres lock contention" in first_user_msg


async def test_knowledge_search_tool():
    from agent_orchestrator.investigator import ToolContext, _dispatch_tool

    store = KnowledgeStore()
    store.add("runbook", "r.md", "Kafka lag", "check consumer group offsets")
    bb = Blackboard()
    session = bb.create("inc-kt", "test")
    ctx = ToolContext(SimpleNamespace(), session, bb, Settings(), store)

    payload = await _dispatch_tool("knowledge_search", {"query": "consumer offsets"}, ctx)
    assert "Kafka lag" in payload
    assert "error" in await _dispatch_tool("knowledge_search", {}, ctx)

    ctx_none = ToolContext(SimpleNamespace(), session, bb, Settings(), None)
    assert "not configured" in await _dispatch_tool(
        "knowledge_search", {"query": "x"}, ctx_none
    )


# ---------------------------------------------------------------------------
# Orchestrator end-to-end learning loop (fake connectors, no infra)
# ---------------------------------------------------------------------------
async def test_orchestrator_records_post_mortem_end_to_end():
    from agent_orchestrator.investigator import LLMResponse
    from agent_orchestrator.models import IncidentState
    from agent_orchestrator.orchestrator import Orchestrator

    terminated = []

    async def terminate_backend(pid):
        terminated.append(pid)
        return True

    connectors = SimpleNamespace(
        postgres=SimpleNamespace(terminate_backend=terminate_backend)
    )

    class InvestigatorLLM:
        async def complete(self, messages, max_tokens):
            return LLMResponse(
                text=json.dumps({
                    "action": "diagnose", "root_cause": "db_lock_contention",
                    "confidence": 0.95, "rationale": "lead blocker pid 42",
                    "evidence": ["[knowledge_search] runbook match"],
                    "proposed_actions": [{
                        "action_type": "terminate_blocking_queries",
                        "target": "postgres", "params": {"pid": 42},
                        "rationale": "kill blocker"}],
                }),
                tokens=50,
            )

    store = KnowledgeStore()
    orch = Orchestrator(
        Settings(investigator_timeout_s=5.0),
        connectors,  # type: ignore[arg-type]
        blackboard=Blackboard(),
        agents=[],  # no findings -> reasoner returns UNKNOWN -> investigator runs
        llm=InvestigatorLLM(),
        knowledge=store,
    )
    session = await orch.handle_incident("test-trigger")

    assert session.state is IncidentState.RESOLVED
    assert terminated == [42]
    hits = store.search("db_lock_contention", kind="incident")
    assert hits and hits[0].ref == session.incident_id
