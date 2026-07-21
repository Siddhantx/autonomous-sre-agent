"""Investigator tests — fake LLM, no network, no live infra."""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import httpx
import pytest

from agent_orchestrator.blackboard import Blackboard
from agent_orchestrator.config import Settings
from agent_orchestrator.investigator import (
    AnthropicClient,
    LLMResponse,
    OpenAICompatibleClient,
    _parse_json,
    _to_diagnosis,
    investigate,
    make_llm_client,
)
from agent_orchestrator.models import (
    ActionType,
    Finding,
    RootCause,
    Severity,
    SubsystemStatus,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------
def settings(**overrides) -> Settings:
    return Settings(
        investigator_max_steps=overrides.pop("max_steps", 4),
        investigator_max_tokens=overrides.pop("max_tokens", 4000),
        investigator_timeout_s=overrides.pop("timeout_s", 5.0),
        **overrides,
    )


def make_session(blackboard: Blackboard):
    session = blackboard.create("inc-test", "test")
    session.findings.append(
        Finding(
            agent_name="db-lock-agent",
            subsystem="postgres",
            status=SubsystemStatus.FAULTED,
            severity=Severity.CRITICAL,
            summary="1 backend blocked by pid 42",
            metrics={"primary_blocking_pid": 42.0},
        )
    )
    return session


class FakeLLM:
    """Replays a script of responses; records every call."""

    def __init__(self, script: list[LLMResponse]):
        self.script = list(script)
        self.calls: list[dict] = []

    async def complete(self, messages, max_tokens) -> LLMResponse:
        self.calls.append({"messages": messages, "max_tokens": max_tokens})
        if not self.script:
            return LLMResponse(text="{}", tokens=10)
        return self.script.pop(0)


def tool_reply(*calls: dict) -> LLMResponse:
    return LLMResponse(
        text=json.dumps({"action": "tools", "calls": list(calls)}), tokens=100
    )


def diagnose_reply(**payload) -> LLMResponse:
    return LLMResponse(
        text=json.dumps({"action": "diagnose", **payload}), tokens=100
    )


def fake_connectors(**kw) -> SimpleNamespace:
    async def blocking_backends():
        return [{"blocked_pid": 7, "blocking_pid": 42}]

    postgres = SimpleNamespace(blocking_backends=blocking_backends, **kw)
    return SimpleNamespace(postgres=postgres)


# ---------------------------------------------------------------------------
# Budget enforcement
# ---------------------------------------------------------------------------
async def test_step_budget_exhaustion_escalates():
    llm = FakeLLM([tool_reply({"tool": "blackboard_context", "args": {}})] * 10)
    bb = Blackboard()
    d = await investigate(make_session(bb), bb, fake_connectors(), settings(max_steps=3), llm)
    assert d.root_cause is RootCause.UNKNOWN
    assert d.confidence == 0.0
    assert "budget exhausted" in d.rationale
    assert len(llm.calls) == 3


async def test_token_budget_exhaustion_escalates():
    llm = FakeLLM(
        [LLMResponse(text=json.dumps({"action": "tools", "calls": []}), tokens=3000)] * 5
    )
    bb = Blackboard()
    d = await investigate(make_session(bb), bb, fake_connectors(), settings(max_tokens=4000), llm)
    assert d.root_cause is RootCause.UNKNOWN
    assert "budget exhausted" in d.rationale
    assert len(llm.calls) == 2  # 3000 + 3000 > 4000 stops the third call


async def test_timeout_escalates():
    class SlowLLM:
        async def complete(self, messages, max_tokens):
            await asyncio.sleep(10)

    bb = Blackboard()
    d = await investigate(
        make_session(bb), bb, fake_connectors(), settings(timeout_s=0.05), SlowLLM()
    )
    assert d.root_cause is RootCause.UNKNOWN
    assert "timed out" in d.rationale


async def test_llm_exception_escalates_never_raises():
    class BrokenLLM:
        async def complete(self, messages, max_tokens):
            raise RuntimeError("provider down")

    bb = Blackboard()
    d = await investigate(make_session(bb), bb, fake_connectors(), settings(), BrokenLLM())
    assert d.root_cause is RootCause.UNKNOWN
    assert "provider down" in d.rationale


# ---------------------------------------------------------------------------
# Whitelist enforcement + escalation on no-fit action
# ---------------------------------------------------------------------------
async def test_non_whitelisted_action_dropped():
    llm = FakeLLM(
        [
            diagnose_reply(
                root_cause="db_lock_contention",
                confidence=0.9,
                rationale="lock storm",
                evidence=["[pg_blocking] pid 42 blocks 7"],
                proposed_actions=[
                    {"action_type": "drop_all_tables", "target": "postgres"},
                    {"action_type": "terminate_blocking_queries", "target": "postgres",
                     "params": {"pid": 42}},
                ],
            )
        ]
    )
    bb = Blackboard()
    d = await investigate(make_session(bb), bb, fake_connectors(), settings(), llm)
    assert [a.action_type for a in d.proposed_actions] == [
        ActionType.TERMINATE_BLOCKING_QUERIES
    ]
    assert "drop_all_tables" in d.rationale  # dropped and noted


async def test_no_fit_action_returns_empty_with_rationale():
    llm = FakeLLM(
        [
            diagnose_reply(
                root_cause="unknown",
                confidence=0.6,
                rationale="disk filling on postgres volume; no whitelisted action fits",
                evidence=["[prometheus_query] disk 95% full"],
                proposed_actions=[],
            )
        ]
    )
    bb = Blackboard()
    d = await investigate(make_session(bb), bb, fake_connectors(), settings(), llm)
    assert d.proposed_actions == []
    assert "no whitelisted action fits" in d.rationale


async def test_unknown_root_cause_string_maps_to_unknown():
    llm = FakeLLM([diagnose_reply(root_cause="alien_invasion", confidence=0.5, rationale="x")])
    bb = Blackboard()
    d = await investigate(make_session(bb), bb, fake_connectors(), settings(), llm)
    assert d.root_cause is RootCause.UNKNOWN


# ---------------------------------------------------------------------------
# Scripted tool-use sequence
# ---------------------------------------------------------------------------
async def test_scripted_tool_sequence_produces_diagnosis():
    llm = FakeLLM(
        [
            tool_reply(
                {"tool": "pg_blocking", "args": {}},
                {"tool": "blackboard_context", "args": {}},
            ),
            diagnose_reply(
                root_cause="db_lock_contention",
                confidence=0.85,
                rationale="pid 42 is the lead blocker",
                evidence=["[pg_blocking] pid 42 blocks pid 7"],
                proposed_actions=[
                    {"action_type": "terminate_blocking_queries", "target": "postgres",
                     "params": {"pid": 42}, "rationale": "kill blocker"}
                ],
            ),
        ]
    )
    bb = Blackboard()
    d = await investigate(make_session(bb), bb, fake_connectors(), settings(), llm)

    assert d.root_cause is RootCause.DB_LOCK_CONTENTION
    assert d.confidence == 0.85
    assert d.proposed_actions[0].params == {"pid": 42}
    assert d.evidence == ["[pg_blocking] pid 42 blocks pid 7"]
    # The second LLM call must contain the tool results from the first.
    second_call_user = llm.calls[1]["messages"][-1]["content"]
    assert "blocking_pid" in second_call_user and "42" in second_call_user


async def test_tool_failure_degrades_into_error_payload():
    async def broken():
        raise ConnectionError("pg down")

    llm = FakeLLM(
        [
            tool_reply({"tool": "pg_blocking", "args": {}}),
            diagnose_reply(root_cause="unknown", confidence=0.1, rationale="no data"),
        ]
    )
    bb = Blackboard()
    connectors = SimpleNamespace(postgres=SimpleNamespace(blocking_backends=broken))
    d = await investigate(make_session(bb), bb, connectors, settings(), llm)
    assert d.root_cause is RootCause.UNKNOWN
    assert "ConnectionError" in llm.calls[1]["messages"][-1]["content"]


async def test_unknown_tool_returns_error_not_crash():
    llm = FakeLLM(
        [
            tool_reply({"tool": "rm_rf_slash", "args": {}}),
            diagnose_reply(root_cause="unknown", confidence=0.0, rationale="x"),
        ]
    )
    bb = Blackboard()
    d = await investigate(make_session(bb), bb, fake_connectors(), settings(), llm)
    assert "unknown tool" in llm.calls[1]["messages"][-1]["content"]
    assert d.root_cause is RootCause.UNKNOWN


async def test_invalid_json_reply_costs_a_step_then_recovers():
    llm = FakeLLM(
        [
            LLMResponse(text="I think the database is locked...", tokens=50),
            diagnose_reply(root_cause="db_lock_contention", confidence=0.8, rationale="x"),
        ]
    )
    bb = Blackboard()
    d = await investigate(make_session(bb), bb, fake_connectors(), settings(), llm)
    assert d.root_cause is RootCause.DB_LOCK_CONTENTION
    assert "Invalid reply" in llm.calls[1]["messages"][-1]["content"]


# ---------------------------------------------------------------------------
# Parsing helpers
# ---------------------------------------------------------------------------
def test_parse_json_tolerates_markdown_fences():
    assert _parse_json('```json\n{"a": 1}\n```') == {"a": 1}
    assert _parse_json('{"a": 1}') == {"a": 1}
    assert _parse_json("not json") is None
    assert _parse_json("[1, 2]") is None  # must be an object


def test_to_diagnosis_clamps_confidence():
    assert _to_diagnosis({"confidence": 5.0, "rationale": ""}).confidence == 1.0
    assert _to_diagnosis({"confidence": -1.0, "rationale": ""}).confidence == 0.0


def test_to_diagnosis_parses_differential_hypotheses():
    d = _to_diagnosis({
        "root_cause": "kafka_poison_pill", "confidence": 0.8, "rationale": "r",
        "hypotheses": [
            {"root_cause": "kafka_poison_pill", "confidence": 0.8,
             "evidence_for": ["lag pinned on partition 0"],
             "evidence_against": []},
            {"root_cause": "kafka_consumer_lag", "confidence": 0.3,
             "evidence_for": ["lag rising"],
             "evidence_against": ["other partitions drain fine"]},
            {"root_cause": "alien_invasion", "confidence": 0.9},  # dropped
            {"root_cause": "disk_fill", "confidence": "not-a-number"},  # dropped
        ],
    })
    assert [h.root_cause for h in d.hypotheses] == [
        RootCause.KAFKA_POISON_PILL, RootCause.KAFKA_CONSUMER_LAG,
    ]
    assert d.hypotheses[1].evidence_against == ["other partitions drain fine"]


# ---------------------------------------------------------------------------
# Tool handlers (fake connectors, no infra)
# ---------------------------------------------------------------------------
async def test_all_tool_handlers_dispatch(tmp_path):
    from agent_orchestrator.investigator import TOOLS, ToolContext, _dispatch_tool

    (tmp_path / "svc").mkdir()
    (tmp_path / "svc" / "app.py").write_text("MAX_CONNECTIONS = 10\n")

    async def fetch(sql, *params):
        return [{"pid": 1, "state": "active"}]

    async def blocking_backends():
        return [{"blocking_pid": 42}]

    async def info():
        return {"used_memory": 1}

    async def slowlog(n):
        return [{"id": 1}]

    async def key_sample(n):
        return [{"key": "k", "type": "string", "ttl": -1}]

    async def total_consumer_lag(g, t):
        return 500

    async def topic_offsets(t):
        return {"topic": t, "partitions": 3}

    async def instant_query(q):
        return 0.5

    async def range_query(q, s, e, st):
        return [{"values": [[1, "0.5"]]}]

    async def loki_search(logql, minutes, limit=50):
        return [{"ts": "1", "container": "order-service", "line": "boom"}]

    async def pod_states(ns=None):
        return [{"name": "p", "phase": "Running", "restarts": 0, "waiting_reasons": []}]

    async def k8s_events(ns=None):
        return [{"type": "Normal", "reason": "Pulled"}]

    connectors = SimpleNamespace(
        k8s=SimpleNamespace(pod_states=pod_states, events=k8s_events),
        postgres=SimpleNamespace(fetch=fetch, blocking_backends=blocking_backends),
        redis=SimpleNamespace(info=info, slowlog=slowlog, key_sample=key_sample),
        kafka=SimpleNamespace(
            total_consumer_lag=total_consumer_lag, topic_offsets=topic_offsets
        ),
        prometheus=SimpleNamespace(
            instant_query=instant_query, range_query=range_query
        ),
        loki=SimpleNamespace(search=loki_search),
    )
    from agent_orchestrator.knowledge import KnowledgeStore

    knowledge = KnowledgeStore()
    knowledge.add("runbook", "r.md", "locks", "terminate blockers")
    bb = Blackboard()
    ctx = ToolContext(
        connectors, make_session(bb), bb, settings(lab_source_path=tmp_path),
        knowledge,
    )

    args_by_tool = {
        "pg_explain": {"query": "SELECT 1"},
        "code_search": {"pattern": "max_connections"},
        "knowledge_search": {"query": "blockers"},
        "log_search": {"service": "order-service", "pattern": "boom"},
        "prometheus_query": {"query": "up"},
        "prometheus_range": {"query": "up", "start": "0", "end": "1", "step": "60s"},
    }
    for name in TOOLS:
        payload = await _dispatch_tool(name, args_by_tool.get(name, {}), ctx)
        assert "error" not in payload.lower(), f"{name}: {payload}"

    # Guard rails on pg_explain and code_search
    assert "error" in await _dispatch_tool("pg_explain", {"query": "DELETE FROM t"}, ctx)
    assert "error" in await _dispatch_tool("pg_explain", {"query": "SELECT 1; DROP TABLE t"}, ctx)
    assert "error" in await _dispatch_tool("code_search", {}, ctx)
    assert "no matches" in await _dispatch_tool(
        "code_search", {"pattern": "zzz_not_present"}, ctx
    )


def test_provider_layer_selection_and_registration():
    from agent_orchestrator.investigator import PROVIDERS, active_tools, register_provider

    # "all" with k8s configured = every registered tool; without = k8s hidden
    every = {n for p in PROVIDERS.values() for n in p}
    assert set(active_tools(settings(tool_providers="all", k8s_api_url="https://k"))) \
        == every
    assert set(active_tools(settings(tool_providers="all"))) \
        == every - set(PROVIDERS["kubernetes"])

    subset = active_tools(settings(tool_providers="postgres, logs"))
    assert "pg_blocking" in subset and "log_search" in subset
    assert "redis_info" not in subset and "kafka_consumer_lag" not in subset

    # Unknown provider names are ignored, not fatal
    assert active_tools(settings(tool_providers="postgres, nope")) == active_tools(
        settings(tool_providers="postgres")
    )

    async def custom_tool(args, ctx):
        return {"ok": True}

    register_provider("cloudwatch", {"cw_metric": custom_tool})
    try:
        assert "cw_metric" in active_tools(settings(tool_providers="cloudwatch"))
        assert "cw_metric" in active_tools(settings(tool_providers="all"))
    finally:
        del PROVIDERS["cloudwatch"]


async def test_loop_respects_provider_subset():
    """A tool outside the enabled providers errors; the prompt only lists enabled."""
    llm = FakeLLM(
        [
            tool_reply({"tool": "redis_info", "args": {}}),
            diagnose_reply(root_cause="unknown", confidence=0.1, rationale="x"),
        ]
    )
    bb = Blackboard()
    d = await investigate(
        make_session(bb), bb, fake_connectors(),
        settings(tool_providers="postgres"), llm,
    )
    assert d.root_cause is RootCause.UNKNOWN
    assert "redis_info" not in llm.calls[0]["messages"][0]["content"]
    assert "unknown tool" in llm.calls[1]["messages"][-1]["content"]


async def test_log_search_builds_safe_logql():
    from agent_orchestrator.investigator import ToolContext, _dispatch_tool

    seen = {}

    async def loki_search(logql, minutes, limit=50):
        seen["logql"] = logql
        seen["minutes"] = minutes
        return []

    bb = Blackboard()
    ctx = ToolContext(
        SimpleNamespace(loki=SimpleNamespace(search=loki_search)),
        make_session(bb), bb, settings(),
    )
    # Service name is sanitized; pattern is escaped as a string literal
    await _dispatch_tool(
        "log_search",
        {"service": 'order"}; drop', "pattern": 'a"b\\c', "minutes": 9999},
        ctx,
    )
    assert seen["logql"] == '{container=~".*orderdrop.*"} |= "a\\"b\\\\c"'
    assert seen["minutes"] == 240  # clamped

    await _dispatch_tool("log_search", {}, ctx)
    assert seen["logql"] == '{container=~".+"}'
    assert seen["minutes"] == 15


async def test_k8s_connector_and_tools():
    from agent_orchestrator.connectors import K8sConnector
    from agent_orchestrator.investigator import ToolContext, _dispatch_tool, active_tools

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/pods"):
            return httpx.Response(200, json={"items": [
                {"metadata": {"name": "payment-7d9f"},
                 "status": {"phase": "Running", "containerStatuses": [
                     {"restartCount": 7,
                      "state": {"waiting": {"reason": "CrashLoopBackOff"}}}]}},
            ]})
        return httpx.Response(200, json={"items": [
            {"type": "Warning", "reason": "BackOff",
             "involvedObject": {"name": "payment-7d9f"},
             "message": "Back-off restarting failed container", "count": 12,
             "lastTimestamp": "2026-07-21T10:00:00Z"},
            {"type": "Normal", "reason": "Pulled",
             "involvedObject": {"name": "order-1"},
             "message": "ok", "count": 1, "lastTimestamp": "2026-07-21T11:00:00Z"},
        ]})

    k8s = K8sConnector(
        settings(k8s_api_url="https://kube.test"),
        client=httpx.AsyncClient(
            transport=httpx.MockTransport(handler), base_url="https://kube.test"
        ),
    )
    pods = await k8s.pod_states()
    assert pods == [{"name": "payment-7d9f", "phase": "Running", "restarts": 7,
                     "waiting_reasons": ["CrashLoopBackOff"]}]
    events = await k8s.events()
    assert events[0]["type"] == "Warning"  # warnings sort first

    # Tools degrade when unconfigured, and the provider hides itself
    bb = Blackboard()
    session = make_session(bb)
    ctx = ToolContext(SimpleNamespace(k8s=None), session, bb, settings())
    assert "not configured" in await _dispatch_tool("k8s_pod_states", {}, ctx)
    assert "k8s_pod_states" not in active_tools(settings())  # no APOE_K8S_API_URL
    assert "k8s_pod_states" in active_tools(settings(k8s_api_url="https://kube.test"))

    ctx_live = ToolContext(SimpleNamespace(k8s=k8s), session, bb, settings())
    assert "CrashLoopBackOff" in await _dispatch_tool("k8s_pod_states", {}, ctx_live)
    assert "BackOff" in await _dispatch_tool("k8s_events", {}, ctx_live)


async def test_loki_connector_query_and_parse():
    from agent_orchestrator.connectors import LokiConnector

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/loki/api/v1/query_range"
        assert request.url.params["direction"] == "backward"
        return httpx.Response(
            200,
            json={"data": {"result": [
                {"stream": {"container": "order-service"},
                 "values": [["100", "older line"], ["200", "newer line"]]},
            ]}},
        )

    connector = LokiConnector(
        settings(),
        client=httpx.AsyncClient(
            transport=httpx.MockTransport(handler), base_url="http://loki:3100"
        ),
    )
    lines = await connector.search('{container=~".+"}', minutes=15)
    assert [entry["line"] for entry in lines] == ["newer line", "older line"]
    assert lines[0]["container"] == "order-service"


# ---------------------------------------------------------------------------
# LLM clients (httpx.MockTransport — no network)
# ---------------------------------------------------------------------------
async def test_anthropic_client_request_and_parse():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["path"] = request.url.path
        seen["body"] = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "content": [{"type": "text", "text": '{"action": "diagnose"}'}],
                "usage": {"input_tokens": 100, "output_tokens": 20},
            },
        )

    client = AnthropicClient(
        settings(llm_model="claude-test"),
        client=httpx.AsyncClient(
            transport=httpx.MockTransport(handler), base_url="https://api.anthropic.com"
        ),
    )
    resp = await client.complete(
        [{"role": "system", "content": "sys"}, {"role": "user", "content": "hi"}],
        max_tokens=500,
    )
    assert resp.text == '{"action": "diagnose"}'
    assert resp.tokens == 120
    assert seen["path"] == "/v1/messages"
    assert seen["body"]["system"] == "sys"
    assert seen["body"]["messages"] == [{"role": "user", "content": "hi"}]


async def test_openai_client_request_and_parse():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/chat/completions")
        return httpx.Response(
            200,
            json={
                "choices": [{"message": {"content": "ok"}}],
                "usage": {"total_tokens": 42},
            },
        )

    client = OpenAICompatibleClient(
        settings(llm_model="llama3", llm_base_url="http://localhost:11434/v1"),
        client=httpx.AsyncClient(
            transport=httpx.MockTransport(handler), base_url="http://localhost:11434/v1"
        ),
    )
    resp = await client.complete([{"role": "user", "content": "hi"}], max_tokens=100)
    assert resp.text == "ok"
    assert resp.tokens == 42


def test_make_llm_client_provider_selection():
    assert isinstance(
        make_llm_client(settings(llm_provider="anthropic", llm_model="m")),
        AnthropicClient,
    )
    assert isinstance(
        make_llm_client(
            settings(llm_provider="openai", llm_model="m", llm_base_url="http://x/v1")
        ),
        OpenAICompatibleClient,
    )
    with pytest.raises(ValueError):
        make_llm_client(settings(llm_provider="bedrock"))
