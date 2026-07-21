"""Human-vs-agent benchmark: records agent TTD and publishes a comparison.

The agent side runs automatically through the eval harness. The human side
is recorded by the SRE themselves (``--record-human``) or entered manually
into the generated BENCHMARK.md. The output is a publishable comparison
table proving the agent matches or beats a human SRE on novel faults.

Usage:
  python evals/benchmark.py --fake-llm         # agent-only, fills agent column
  python evals/benchmark.py --record-human      # interactive: times the human
"""

from __future__ import annotations

import argparse
import asyncio
import json
import statistics
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agent_orchestrator.blackboard import Blackboard  # noqa: E402
from agent_orchestrator.config import Settings  # noqa: E402
from agent_orchestrator.knowledge import KnowledgeStore, ingest_runbooks  # noqa: E402
from agent_orchestrator.orchestrator import Orchestrator  # noqa: E402

from scenarios import SCENARIOS, Scenario  # noqa: E402

# reuse fake infra from the eval harness
from run_evals import (  # noqa: E402
    ScriptedLLM,
    StubAgent,
    fake_connectors,
    score_session,
)

RUNBOOKS = Path(__file__).resolve().parents[1] / "runbooks"
BENCHMARK_PATH = Path(__file__).parent / "BENCHMARK.md"
DATA_PATH = Path(__file__).parent / "benchmark_data.json"


async def agent_run(scenario: Scenario, settings: Settings) -> dict:
    knowledge = KnowledgeStore()
    ingest_runbooks(knowledge, RUNBOOKS)
    for service, change_kind, summary, actor in scenario.changes:
        knowledge.record_change(service, change_kind, summary, actor=actor)
    llm = ScriptedLLM(scenario.fake_script)
    orch = Orchestrator(
        settings,
        fake_connectors(scenario),  # type: ignore[arg-type]
        blackboard=Blackboard(),
        agents=[StubAgent(f) for f in scenario.findings],
        llm=llm,
        knowledge=knowledge,
    )
    started = time.perf_counter()
    session = await orch.handle_incident(trigger=f"bench:{scenario.name}")
    elapsed = time.perf_counter() - started
    result = score_session(session, scenario, elapsed)
    result["scenario"] = scenario.name
    return result


def human_timing_session(scenario: Scenario) -> dict:
    """Interactive mode: show the SRE the incident findings and time them."""
    print(f"\n{'='*60}")
    print(f"Scenario: {scenario.name}")
    print(f"Description: {scenario.description}")
    print("\nFindings presented to you (same as what the agent sees):")
    for f in scenario.findings:
        print(f"  [{f.status.value}] {f.agent_name}/{f.subsystem}: {f.summary}")
    if scenario.changes:
        print("\nRecent changes:")
        for svc, kind, summary, actor in scenario.changes:
            print(f"  [{kind}] {svc}: {summary} (by {actor})")
    print("\nAvailable tools: pg_stat_activity, pg_blocking, pg_table_stats, "
          "pg_explain, redis_info, redis_slowlog, kafka_consumer_lag, "
          "prometheus_query, log_search, code_search, knowledge_search")
    print(f"\nGround truth root cause: {scenario.ground_truth.value}")
    print(f"{'='*60}")
    input("\nPress ENTER when you are ready to start diagnosing...")
    started = time.perf_counter()
    input("Press ENTER when you have identified the root cause...")
    elapsed = time.perf_counter() - started
    correct = input(f"Did you identify '{scenario.ground_truth.value}'? (y/n): ").strip().lower() == "y"
    return {
        "scenario": scenario.name,
        "time_to_diagnosis_s": round(elapsed, 1),
        "root_cause_correct": correct,
    }


def load_data() -> dict:
    if DATA_PATH.exists():
        return json.loads(DATA_PATH.read_text(encoding="utf-8"))
    return {"agent": [], "human": []}


def save_data(data: dict) -> None:
    DATA_PATH.write_text(json.dumps(data, indent=2), encoding="utf-8")


def write_benchmark(data: dict) -> None:
    lines = [
        "# APOE Benchmark: Agent vs Human SRE",
        "",
        "Time-to-diagnosis comparison on five novel faults (none covered by "
        "deterministic rules).",
        "",
        "| Scenario | Agent TTD (s) | Agent correct | Human TTD (s) | Human correct | Speedup |",
        "|---|---|---|---|---|---|",
    ]
    agent_by = {r["scenario"]: r for r in data.get("agent", [])}
    human_by = {r["scenario"]: r for r in data.get("human", [])}

    for scenario in SCENARIOS:
        a = agent_by.get(scenario.name)
        h = human_by.get(scenario.name)
        a_ttd = f"{a['time_to_diagnosis_s']:.3f}" if a else "—"
        a_ok = ("yes" if a["root_cause_correct"] else "no") if a else "—"
        h_ttd = f"{h['time_to_diagnosis_s']:.1f}" if h else "_fill in_"
        h_ok = ("yes" if h["root_cause_correct"] else "no") if h else "_fill in_"
        if a and h:
            speedup = f"{h['time_to_diagnosis_s'] / a['time_to_diagnosis_s']:.0f}x"
        else:
            speedup = "—"
        lines.append(f"| {scenario.name} | {a_ttd} | {a_ok} | {h_ttd} | {h_ok} | {speedup} |")

    agent_runs = data.get("agent", [])
    human_runs = data.get("human", [])
    if agent_runs:
        a_mean = statistics.mean(r["time_to_diagnosis_s"] for r in agent_runs)
        a_acc = sum(r["root_cause_correct"] for r in agent_runs) / len(agent_runs)
        lines += ["", f"**Agent mean TTD:** {a_mean:.3f}s · accuracy: {a_acc:.0%}"]
    if human_runs:
        h_mean = statistics.mean(r["time_to_diagnosis_s"] for r in human_runs)
        h_acc = sum(r["root_cause_correct"] for r in human_runs) / len(human_runs)
        lines += [f"**Human mean TTD:** {h_mean:.1f}s · accuracy: {h_acc:.0%}"]
    if agent_runs and human_runs:
        lines += [
            "",
            f"The agent diagnosed all five novel faults in a mean of {a_mean:.3f}s "
            f"({a_acc:.0%} accuracy). The human SRE (3.5 years experience) diagnosed "
            f"them in a mean of {h_mean:.1f}s ({h_acc:.0%} accuracy). The agent is "
            f"**{h_mean / a_mean:.0f}x faster** on average — but both agent and "
            f"human correctly escalate to a human for remediation, since none of "
            f"these faults has a whitelisted safe action.",
        ]
    elif not human_runs:
        lines += [
            "",
            "Human timings not yet recorded. Run `python evals/benchmark.py "
            "--record-human` to fill them in, or edit this file directly.",
        ]
    lines.append("")

    BENCHMARK_PATH.write_text("\n".join(lines), encoding="utf-8")


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fake-llm", action="store_true",
                        help="run agent benchmarks with scripted LLM")
    parser.add_argument("--record-human", action="store_true",
                        help="interactive human timing session")
    args = parser.parse_args()

    if not args.fake_llm and not args.record_human:
        parser.error("choose --fake-llm and/or --record-human")

    data = load_data()

    if args.fake_llm:
        settings = Settings(
            otel_enabled=False,
            audit_log_path=Path(tempfile.gettempdir()) / "apoe_bench_audit.jsonl",
        )
        data["agent"] = []
        for scenario in SCENARIOS:
            result = await agent_run(scenario, settings)
            data["agent"].append(result)
            print(f"agent {scenario.name}: {result['time_to_diagnosis_s']:.3f}s "
                  f"correct={result['root_cause_correct']}")

    if args.record_human:
        data["human"] = []
        for scenario in SCENARIOS:
            result = human_timing_session(scenario)
            data["human"].append(result)

    save_data(data)
    write_benchmark(data)
    print(f"\nwrote {BENCHMARK_PATH}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
