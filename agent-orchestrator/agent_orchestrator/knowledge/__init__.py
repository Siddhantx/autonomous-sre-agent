"""Knowledge layer: SQLite FTS5 store + ingestion + incident learning loop."""

from .ingest import (
    ingest_all,
    ingest_git_history,
    ingest_lab_sources,
    ingest_runbooks,
)
from .store import KnowledgeHit, KnowledgeStore

__all__ = [
    "KnowledgeHit",
    "KnowledgeStore",
    "ingest_all",
    "ingest_git_history",
    "ingest_lab_sources",
    "ingest_runbooks",
]
