"""Ingestion: lab service sources, compose topology, runbooks -> the store.

Idempotent — documents are upserted by (kind, ref), so re-running ingestion
refreshes rather than duplicates.
"""

from __future__ import annotations

from pathlib import Path

from ..config import Settings
from .store import KnowledgeStore

_CODE_SUFFIXES = {".py", ".js", ".ts", ".sql", ".conf"}
_TOPOLOGY_SUFFIXES = {".yml", ".yaml"}
_MAX_FILE_BYTES = 200_000


def ingest_lab_sources(store: KnowledgeStore, lab_path: Path) -> int:
    """Index service source code and compose topology under the lab tree."""
    count = 0
    if not lab_path.is_dir():
        return 0
    for path in sorted(lab_path.rglob("*")):
        if not path.is_file() or path.stat().st_size > _MAX_FILE_BYTES:
            continue
        if path.suffix in _CODE_SUFFIXES:
            kind = "code"
        elif path.suffix in _TOPOLOGY_SUFFIXES:
            kind = "topology"
        else:
            continue
        rel = str(path.relative_to(lab_path))
        store.add(kind, rel, path.name,
                  path.read_text(encoding="utf-8", errors="ignore"))
        count += 1
    return count


def ingest_runbooks(store: KnowledgeStore, runbooks_path: Path) -> int:
    """Index every markdown runbook; title is the first # heading."""
    count = 0
    if not runbooks_path.is_dir():
        return 0
    for md in sorted(runbooks_path.glob("*.md")):
        text = md.read_text(encoding="utf-8", errors="ignore")
        title = next(
            (line.lstrip("# ").strip() for line in text.splitlines()
             if line.startswith("#")),
            md.stem,
        )
        store.add("runbook", md.name, title, text)
        count += 1
    return count


def ingest_all(store: KnowledgeStore, settings: Settings) -> dict[str, int]:
    return {
        "lab_documents": ingest_lab_sources(store, settings.lab_source_path),
        "runbooks": ingest_runbooks(store, settings.runbooks_path),
    }
