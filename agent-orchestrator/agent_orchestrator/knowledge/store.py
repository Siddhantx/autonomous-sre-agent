"""SQLite FTS5 knowledge store.

One table, four namespaced kinds (the agentdb memory-pattern split):

* ``code``     — lab service sources (semantic memory),
* ``topology`` — docker-compose deployment structure,
* ``runbook``  — human-written failure-mode playbooks,
* ``incident`` — structured post-mortems of past sessions (episodic memory).

stdlib ``sqlite3`` + FTS5, ranked by bm25. No embeddings, no network, no new
dependency — deliberately air-gap friendly.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path

from ..models import IncidentSession


@dataclass(frozen=True)
class KnowledgeHit:
    kind: str
    ref: str
    title: str
    snippet: str


class KnowledgeStore:
    def __init__(self, path: str | Path = ":memory:") -> None:
        # ponytail: check_same_thread=False + no lock; single asyncio event
        # loop today. Move to a connection pool if the API grows worker threads.
        self._conn = sqlite3.connect(str(path), check_same_thread=False)
        self._conn.execute(
            "CREATE VIRTUAL TABLE IF NOT EXISTS knowledge "
            "USING fts5(kind, ref, title, content)"
        )

    def add(self, kind: str, ref: str, title: str, content: str) -> None:
        """Upsert one document, keyed by (kind, ref)."""
        self._conn.execute(
            "DELETE FROM knowledge WHERE kind = ? AND ref = ?", (kind, ref)
        )
        self._conn.execute(
            "INSERT INTO knowledge (kind, ref, title, content) VALUES (?, ?, ?, ?)",
            (kind, ref, title, content),
        )
        self._conn.commit()

    def search(
        self, query: str, kind: str | None = None, limit: int = 5
    ) -> list[KnowledgeHit]:
        """bm25-ranked full-text search. Query terms are OR-ed for recall."""
        terms = [t.replace('"', "") for t in query.split()]
        terms = [t for t in terms if t]
        if not terms:
            return []
        match = " OR ".join(f'"{t}"' for t in terms)
        sql = (
            "SELECT kind, ref, title, "
            "snippet(knowledge, 3, '[', ']', '…', 24) "
            "FROM knowledge WHERE knowledge MATCH ?"
        )
        params: list[str | int] = [match]
        if kind is not None:
            sql += " AND kind = ?"
            params.append(kind)
        sql += " ORDER BY rank LIMIT ?"
        params.append(limit)
        rows = self._conn.execute(sql, params).fetchall()
        return [KnowledgeHit(*row) for row in rows]

    def add_post_mortem(self, session: IncidentSession) -> None:
        """Append the structured outcome of one incident (the learning loop)."""
        d = session.diagnosis
        record = {
            "incident_id": session.incident_id,
            "trigger": session.trigger,
            "final_state": session.state.value,
            "root_cause": d.root_cause.value if d else None,
            "confidence": d.confidence if d else None,
            "rationale": d.rationale if d else None,
            "evidence": list(d.evidence) if d else [],
            "actions": [
                {"action": r.action.action_type.value, "target": r.action.target,
                 "status": r.status.value, "detail": r.detail}
                for r in session.results
            ],
            "created_at": session.created_at.isoformat(),
            "closed_at": session.updated_at.isoformat(),
        }
        root = record["root_cause"] or "unknown"
        title = f"{root} -> {session.state.value} ({session.trigger})"
        searchable = " ".join(
            filter(None, [root, session.state.value, record["rationale"] or "",
                          *record["evidence"]])
        )
        self.add(
            "incident",
            session.incident_id,
            title,
            f"{searchable}\n{json.dumps(record, default=str)}",
        )

    def close(self) -> None:
        self._conn.close()
