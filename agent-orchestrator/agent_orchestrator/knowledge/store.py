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
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

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

    def record_change(
        self,
        service: str,
        change_kind: str,
        summary: str,
        actor: str = "unknown",
        at: str | None = None,
    ) -> str:
        """Record one change event (deploy, config, schema, infra...).

        Change events answer the first on-call question: "what changed?"
        """
        change_id = f"chg-{uuid.uuid4().hex[:10]}"
        record = {
            "change_id": change_id,
            "service": service,
            "change_kind": change_kind,
            "summary": summary,
            "actor": actor,
            "at": at or datetime.now(tz=timezone.utc).isoformat(),
        }
        self.add(
            "change",
            change_id,
            f"{service} {change_kind}",
            f"{service} {change_kind} {summary} {actor}\n{json.dumps(record)}",
        )
        return change_id

    def recent_changes(
        self, n: int = 10, service: str | None = None
    ) -> list[dict[str, Any]]:
        """Newest change events first (rowid tracks insert order)."""
        sql = "SELECT content FROM knowledge WHERE kind = 'change'"
        params: list[str | int] = []
        if service:
            sql += " AND title LIKE ?"
            params.append(f"{service}%")
        sql += " ORDER BY rowid DESC LIMIT ?"
        params.append(n)
        rows = self._conn.execute(sql, params).fetchall()
        return [json.loads(row[0].splitlines()[-1]) for row in rows]

    def similar_incidents(self, query: str, n: int = 3) -> list[dict[str, Any]]:
        """Past incidents matching the query, WITH their outcomes.

        Outcome-aware retrieval: what actually resolved (or failed) before
        ranks above generic prose when the investigator forms hypotheses.
        """
        terms = [t.replace('"', "") for t in query.split() if t.replace('"', "")]
        if not terms:
            return []
        match = " OR ".join(f'"{t}"' for t in terms)
        rows = self._conn.execute(
            "SELECT content FROM knowledge WHERE knowledge MATCH ? "
            "AND kind = 'incident' ORDER BY rank LIMIT ?",
            (match, n),
        ).fetchall()
        records = []
        for row in rows:
            try:
                records.append(json.loads(row[0].splitlines()[-1]))
            except (json.JSONDecodeError, IndexError):
                continue
        return records

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
        root = (d.root_cause.value if d else None) or "unknown"
        title = f"{root} -> {session.state.value} ({session.trigger})"
        parts: list[str] = [root, session.state.value]
        if d and d.rationale:
            parts.append(d.rationale)
        parts.extend(str(e) for e in (d.evidence if d else []))
        searchable = " ".join(parts)
        self.add(
            "incident",
            session.incident_id,
            title,
            f"{searchable}\n{json.dumps(record, default=str)}",
        )

    def close(self) -> None:
        self._conn.close()
