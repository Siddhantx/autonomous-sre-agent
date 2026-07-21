"""Safety policy compiler.

Autonomous remediation at a Fortune-500 bank is unacceptable without a guard
that a human can read, version and audit. This module compiles a *declarative*
policy (YAML) into fast in-memory predicates and evaluates every proposed
action against them, producing an auditable :class:`SafetyVerdict`.

Design choices that matter for an enterprise reviewer:

* **Default-deny.** If no rule explicitly allows an action, it is blocked. The
  policy's ``default_effect`` may only *loosen* to allow when set deliberately.
* **First-match-wins, ordered.** Deterministic and explainable.
* **Confidence gating.** A rule can require the diagnosis confidence to clear a
  floor before an allow applies — low-confidence diagnoses cannot trigger
  destructive actions.
"""

from __future__ import annotations

from dataclasses import dataclass
from fnmatch import fnmatch
from pathlib import Path

import yaml
from pydantic import BaseModel, Field

from .models import ProposedAction, SafetyVerdict


class PolicyMatch(BaseModel):
    action_types: list[str] = Field(default_factory=lambda: ["*"])
    targets: list[str] = Field(default_factory=lambda: ["*"])
    min_confidence: float = 0.0


class PolicyRule(BaseModel):
    name: str
    effect: str  # "allow" | "deny"
    match: PolicyMatch = Field(default_factory=PolicyMatch)

    def matches(self, action: ProposedAction, confidence: float) -> bool:
        if confidence < self.match.min_confidence:
            return False
        if not any(
            fnmatch(action.action_type.value, pat) for pat in self.match.action_types
        ):
            return False
        return any(fnmatch(action.target, pat) for pat in self.match.targets)


class SafetyPolicy(BaseModel):
    version: int = 1
    default_effect: str = "deny"
    rules: list[PolicyRule] = Field(default_factory=list)


@dataclass(frozen=True)
class CompiledPolicy:
    """A ready-to-evaluate policy. Produced by :func:`compile_policy`."""

    policy: SafetyPolicy

    def evaluate(self, action: ProposedAction, confidence: float) -> SafetyVerdict:
        for rule in self.policy.rules:
            if rule.matches(action, confidence):
                effect = rule.effect.lower()
                return SafetyVerdict(
                    action=action,
                    allowed=effect == "allow",
                    requires_approval=effect == "approval_required",
                    policy=rule.name,
                    reason=(
                        f"rule '{rule.name}' -> {effect} for "
                        f"{action.action_type.value} on {action.target} "
                        f"(confidence {confidence:.2f})"
                    ),
                )
        effect = self.policy.default_effect.lower()
        return SafetyVerdict(
            action=action,
            allowed=effect == "allow",
            requires_approval=effect == "approval_required",
            policy="__default__",
            reason=f"no rule matched; default_effect={self.policy.default_effect}",
        )


def compile_policy(policy: SafetyPolicy) -> CompiledPolicy:
    """Validate and compile a policy. Raises on a policy that cannot be honoured."""
    valid = {"allow", "deny", "approval_required"}
    if policy.default_effect.lower() not in valid:
        raise ValueError(f"invalid default_effect: {policy.default_effect}")
    for rule in policy.rules:
        if rule.effect.lower() not in valid:
            raise ValueError(f"rule '{rule.name}' has invalid effect: {rule.effect}")
    return CompiledPolicy(policy=policy)


def load_policy(path: str | Path) -> CompiledPolicy:
    raw = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    return compile_policy(SafetyPolicy.model_validate(raw))
