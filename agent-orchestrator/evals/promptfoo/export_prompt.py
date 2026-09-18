"""Export the live investigator system prompt for Promptfoo.

Promptfoo needs the prompt as a file. Pasting a copy into the config would
create two sources of truth that drift apart silently — and a prompt
regression suite testing a stale copy of the prompt is worse than no suite,
because it reports green while the real prompt rots.

So the prompt is exported from the running code every time the suite runs.

    python evals/promptfoo/export_prompt.py
"""

from __future__ import annotations

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from agent_orchestrator.investigator import TOOLS, _system_prompt  # noqa: E402

OUT = Path(__file__).parent / "system-prompt.txt"


def export(path: Path | None = None) -> Path:
    target = path or OUT
    target.write_text(_system_prompt(sorted(TOOLS)), encoding="utf-8")
    return target


if __name__ == "__main__":
    written = export()
    print(f"wrote {written} ({written.stat().st_size} bytes)")
