"""CLI for the GenAI evaluation layer.

    # baseline: no LLM, no network, no spend — this is what CI runs
    python evals/llm_eval/run.py

    # real capability, local and air-gapped
    python evals/llm_eval/run.py --model qwen2.5:3b

    # a subset while iterating
    python evals/llm_eval/run.py --only healthy-noise,db-lock-contention

Exit code is 1 if the safety invariant is violated. Accuracy is *reported*,
not gated: a weak model should produce a low score and a green build, because
the thing CI must protect is safety, not cleverness.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from pathlib import Path

_EVALS = Path(__file__).resolve().parents[1]      # evals/
_ROOT = Path(__file__).resolve().parents[2]       # agent-orchestrator/
for _p in (_ROOT, _EVALS):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from llm_eval.dataset import load_benchmark  # noqa: E402
from llm_eval.report import write_report  # noqa: E402
from llm_eval.runner import run_benchmark  # noqa: E402

DEFAULT_OUT = _EVALS / "reports"


def _quiet_logs() -> None:
    """The agent is chatty; the scorecard is the output that matters here."""
    import structlog

    logging.disable(logging.INFO)
    structlog.configure(
        wrapper_class=structlog.make_filtering_bound_logger(logging.WARNING)
    )


async def _run_redteam(out_dir: Path) -> int:
    """Adversarial suite. Exit 1 on any breach — this one always gates."""
    from llm_eval.redteam import run_redteam, to_markdown

    results = await run_redteam()
    out_dir.mkdir(parents=True, exist_ok=True)
    report = out_dir / "redteam.md"
    report.write_text(to_markdown(results), encoding="utf-8")

    breached = [r for r in results if not r.held]
    for result in breached:
        print(f"BREACH {result.attack_id}: {'; '.join(result.violations)}")
    print(f"\n{len(results) - len(breached)}/{len(results)} attacks repelled")
    print(f"wrote {report}")
    if breached:
        print("\nRED-TEAM GATE FAILED — hostile model output reached infrastructure")
        return 1
    return 0


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--model",
        help="real model to evaluate (e.g. qwen2.5:3b). Omit for the "
             "rules-only baseline.",
    )
    parser.add_argument(
        "--base-url", default="http://localhost:11434/v1",
        help="OpenAI-compatible endpoint (default: local Ollama)",
    )
    parser.add_argument("--only", help="comma-separated scenario ids")
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    parser.add_argument(
        "--redteam", action="store_true",
        help="run the adversarial suite instead of the benchmark. Fully "
             "deterministic (scripted attack payloads), so it needs no model "
             "and is safe to gate CI on.",
    )
    parser.add_argument("--quiet", action="store_true", default=True)
    parser.add_argument("--verbose", dest="quiet", action="store_false")
    args = parser.parse_args()

    if args.quiet:
        _quiet_logs()

    if args.redteam:
        return await _run_redteam(args.out)

    only = [s.strip() for s in args.only.split(",")] if args.only else None
    if only:
        known = {s.id for s in load_benchmark().scenarios}
        unknown = sorted(set(only) - known)
        if unknown:
            parser.error(f"unknown scenario id(s): {unknown}")

    result = await run_benchmark(
        model=args.model, base_url=args.base_url, only=only
    )
    json_path, md_path = write_report(result, args.out)

    passed = sum(1 for r in result.runs if r.passed)
    unsafe = [
        r.scenario_id for r in result.runs
        if not next(m for m in r.metrics if m.name == "unsafe_actions").passed
    ]

    print(f"\nmode={result.mode}  model={result.model}")
    print(f"scenarios passing every metric: {passed}/{len(result.runs)}")
    print(f"unsafe actions: {'0 (invariant holds)' if not unsafe else unsafe}")
    print(f"\nwrote {md_path}\nwrote {json_path}")

    if unsafe:
        print("\nSAFETY GATE FAILED — an action executed without an allowing verdict")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
