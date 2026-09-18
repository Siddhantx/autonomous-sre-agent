"""The two CI workflows must not drift apart.

The published repo is produced by `git subtree split -P apoe-monorepo-V1`, so
a workflow at the monorepo root is outside the published prefix and never
ships — the public repo would have no CI at all. Hence two files:

    /.github/workflows/ci.yml                   monorepo
    /apoe-monorepo-V1/.github/workflows/ci.yml  published repo

They differ ONLY in paths and push triggers. Duplication was the accepted
cost of publishing CI; this test is what keeps that cost bounded, by failing
if a job or step is added to one and not the other.

Skips cleanly when run from the published repo, where only one file exists.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

# tests/ -> agent-orchestrator/ -> apoe-monorepo-V1/ -> repo root
_PREFIX_ROOT = Path(__file__).resolve().parents[2]
_MONOREPO_ROOT = _PREFIX_ROOT.parent

PUBLISHED_CI = _PREFIX_ROOT / ".github" / "workflows" / "ci.yml"
MONOREPO_CI = _MONOREPO_ROOT / ".github" / "workflows" / "ci.yml"

pytestmark = pytest.mark.skipif(
    not MONOREPO_CI.exists(),
    reason="only one workflow exists in the published repository",
)


def _load(path: Path) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def test_published_workflow_exists_inside_the_subtree_prefix() -> None:
    """Without this file the published repo ships with no CI whatsoever."""
    assert PUBLISHED_CI.exists(), (
        "the published repo would have no CI: a workflow at the monorepo root "
        "is outside the subtree prefix and is never published"
    )


def test_both_workflows_define_the_same_jobs() -> None:
    assert set(_load(MONOREPO_CI)["jobs"]) == set(_load(PUBLISHED_CI)["jobs"])


def test_every_job_has_the_same_steps_in_the_same_order() -> None:
    mono, published = _load(MONOREPO_CI), _load(PUBLISHED_CI)
    for job in mono["jobs"]:
        mono_steps = [s.get("name") or s.get("uses") for s in mono["jobs"][job]["steps"]]
        pub_steps = [
            s.get("name") or s.get("uses") for s in published["jobs"][job]["steps"]
        ]
        assert mono_steps == pub_steps, f"job {job!r} drifted"


def test_run_commands_are_identical() -> None:
    """Paths live in working-directory and cache keys, never in the commands,
    so the actual build steps must match character for character."""
    mono, published = _load(MONOREPO_CI), _load(PUBLISHED_CI)
    for job in mono["jobs"]:
        mono_runs = [s["run"] for s in mono["jobs"][job]["steps"] if "run" in s]
        pub_runs = [s["run"] for s in published["jobs"][job]["steps"] if "run" in s]
        assert mono_runs == pub_runs, f"job {job!r} has different run commands"


def test_published_workflow_uses_published_paths() -> None:
    published = _load(PUBLISHED_CI)
    assert published["defaults"]["run"]["working-directory"] == "agent-orchestrator"
    flat = yaml.safe_dump(published)
    assert "apoe-monorepo-V1" not in flat, (
        "published workflow still carries a monorepo path; it would break at "
        "the published repo root"
    )


def test_monorepo_workflow_uses_monorepo_paths() -> None:
    mono = _load(MONOREPO_CI)
    assert (
        mono["defaults"]["run"]["working-directory"]
        == "apoe-monorepo-V1/agent-orchestrator"
    )


def test_both_keep_the_redteam_gate() -> None:
    """The one eval step that fails the build must exist in both."""
    for path in (MONOREPO_CI, PUBLISHED_CI):
        runs = " ".join(
            s.get("run", "")
            for job in _load(path)["jobs"].values()
            for s in job["steps"]
        )
        assert "--redteam" in runs, f"{path.name} lost the red-team gate"
