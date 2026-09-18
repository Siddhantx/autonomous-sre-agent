"""Promptfoo suite wiring.

The one property that matters: the suite must test the LIVE system prompt.
A prompt regression suite that reports green against a stale copy of the
prompt is worse than no suite, because it manufactures false confidence in
exactly the artifact it exists to protect.
"""

from __future__ import annotations

import sys
from pathlib import Path

import yaml

_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT / "evals" / "promptfoo"))

from export_prompt import export  # noqa: E402

from agent_orchestrator.investigator import TOOLS, _system_prompt  # noqa: E402

CONFIG = _ROOT / "evals" / "promptfoo" / "promptfooconfig.yaml"


def test_exported_prompt_is_the_live_prompt(tmp_path: Path) -> None:
    """No copy, no drift: the export must equal what the agent actually uses."""
    written = export(tmp_path / "system-prompt.txt")
    assert written.read_text(encoding="utf-8") == _system_prompt(sorted(TOOLS))


def test_exported_prompt_lists_the_real_tool_registry(tmp_path: Path) -> None:
    text = export(tmp_path / "p.txt").read_text(encoding="utf-8")
    for tool in ("pg_stat_activity", "log_search", "knowledge_search"):
        assert tool in text, f"{tool} missing from the exported prompt"


def test_config_reads_the_exported_file_not_an_inline_copy() -> None:
    config = yaml.safe_load(CONFIG.read_text(encoding="utf-8"))
    assert config["prompts"] == ["file://system-prompt.txt"]


def test_every_test_case_inherits_the_protocol_contract() -> None:
    """defaultTest must pin valid JSON and the tools|diagnose contract."""
    config = yaml.safe_load(CONFIG.read_text(encoding="utf-8"))
    asserts = config["defaultTest"]["assert"]
    assert any(a["type"] == "is-json" for a in asserts)
    joined = " ".join(str(a.get("value", "")) for a in asserts)
    assert "tools" in joined and "diagnose" in joined


def test_whitelist_assertion_matches_the_real_action_enum() -> None:
    """The promptfoo whitelist must not drift from ActionType."""
    from agent_orchestrator.models import ActionType

    config = yaml.safe_load(CONFIG.read_text(encoding="utf-8"))
    joined = " ".join(
        str(a.get("value", "")) for a in config["defaultTest"]["assert"]
    )
    for action in ActionType:
        assert action.value in joined, (
            f"{action.value} missing from the promptfoo whitelist assertion"
        )


def test_suite_covers_injection_and_fabrication() -> None:
    config = yaml.safe_load(CONFIG.read_text(encoding="utf-8"))
    descriptions = " ".join(t["description"] for t in config["tests"]).lower()
    assert "injection" in descriptions
    assert "fabricat" in descriptions
