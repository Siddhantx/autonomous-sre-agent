"""Unit tests for the safety policy compiler."""

from __future__ import annotations

import pytest

from agent_orchestrator.models import ActionType, ProposedAction
from agent_orchestrator.safety import (
    PolicyMatch,
    PolicyRule,
    SafetyPolicy,
    compile_policy,
    load_policy,
)
from agent_orchestrator.config import get_settings


def _action(
    action_type: ActionType = ActionType.TERMINATE_BLOCKING_QUERIES,
    target: str = "postgres",
    **params,
) -> ProposedAction:
    return ProposedAction(action_type=action_type, target=target, params=params)


def _policy(**kwargs) -> SafetyPolicy:
    return SafetyPolicy(**kwargs)


def test_default_deny_blocks_unmatched_action():
    compiled = compile_policy(_policy(default_effect="deny", rules=[]))
    verdict = compiled.evaluate(_action(), confidence=1.0)
    assert verdict.allowed is False
    assert verdict.policy == "__default__"


def test_allow_rule_permits_matching_action():
    compiled = compile_policy(
        _policy(
            rules=[
                PolicyRule(
                    name="allow-term",
                    effect="allow",
                    match=PolicyMatch(
                        action_types=["terminate_blocking_queries"],
                        targets=["postgres"],
                        min_confidence=0.9,
                    ),
                )
            ]
        )
    )
    assert compiled.evaluate(_action(pid=1), confidence=0.95).allowed is True


def test_confidence_floor_blocks_low_confidence():
    compiled = compile_policy(
        _policy(
            rules=[
                PolicyRule(
                    name="allow-term",
                    effect="allow",
                    match=PolicyMatch(
                        action_types=["terminate_blocking_queries"],
                        min_confidence=0.9,
                    ),
                )
            ]
        )
    )
    # Below the floor -> rule does not match -> default deny.
    v = compiled.evaluate(_action(pid=1), confidence=0.5)
    assert v.allowed is False
    assert v.policy == "__default__"


def test_first_match_wins_ordering():
    compiled = compile_policy(
        _policy(
            default_effect="deny",
            rules=[
                PolicyRule(name="deny-all", effect="deny",
                           match=PolicyMatch(action_types=["*"], targets=["*"])),
                PolicyRule(name="allow-all", effect="allow",
                           match=PolicyMatch(action_types=["*"], targets=["*"])),
            ],
        )
    )
    v = compiled.evaluate(_action(), confidence=1.0)
    assert v.allowed is False
    assert v.policy == "deny-all"  # earlier rule wins


def test_invalid_effect_rejected_at_compile():
    with pytest.raises(ValueError):
        compile_policy(_policy(default_effect="maybe"))
    with pytest.raises(ValueError):
        compile_policy(
            _policy(rules=[PolicyRule(name="x", effect="permit")])
        )


def test_shipped_policy_allows_db_lock_but_denies_cache_flush():
    compiled = load_policy(get_settings().safety_policy_path)
    term = compiled.evaluate(_action(pid=1), confidence=0.95)
    assert term.allowed is True

    flush = compiled.evaluate(
        _action(action_type=ActionType.FLUSH_CACHE_KEY, target="redis", key="k"),
        confidence=1.0,
    )
    assert flush.allowed is False
