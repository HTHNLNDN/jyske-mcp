"""SQLite-backed institutional-memory graph for research findings and decisions.

Stores three kinds of nodes (Library, API, Concept), the Sources that back
them, and append-only Decision records — plus the edges linking them. Read
via `get_context`, written via `record_facts` / `record_decision`.

This used to be backed by Kuzu (an embedded graph database). Kuzu Inc. was
acquired by Apple, its repo archived Oct 2025, and it was frozen at 0.11.3
with no guaranteed future Python-version compatibility (it ships as a
compiled wheel). A design review also confirmed the actual query patterns
here — fuzzy substring matching plus one-hop lookups from a fact node to its
sources — never do multi-hop graph traversal, so a native graph engine was
never load-bearing. The graph is instead modeled relationally: typed node
rows (`node`), alias rows (`node_alias`), and typed edge tables (`cites`,
`informed_by`, `depends_on`, `relates_to`) on plain stdlib `sqlite3`. This is
zero new dependency, zero compiled-wheel risk, and still a legitimate
knowledge graph — it just isn't stored in a bespoke graph engine.
"""

import json
import logging
import re
import sqlite3
from datetime import datetime, timezone
from hashlib import sha1
from pathlib import Path

from schema import ResearchFacts

log = logging.getLogger("research_graph")

# Deliberately duplicated from jyske_mcp.config (one line) so this dev tooling
# has no import dependency on the product package.
CONFIG_DIR = Path("~/.config/mcp-bank").expanduser()

RESEARCH_GRAPH_FILE = CONFIG_DIR / "research_graph.sqlite"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS node (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    label TEXT NOT NULL,              -- 'Library' | 'API' | 'Concept'
    node_key TEXT NOT NULL,           -- normalized lowercase match key
    name TEXT NOT NULL,               -- display name
    summary TEXT,
    verdict TEXT,                     -- Library only: adopted|rejected|trial|considering
    quirks TEXT,                      -- API only
    updated_at TEXT NOT NULL,
    UNIQUE(label, node_key)
);
CREATE TABLE IF NOT EXISTS node_alias (
    node_id INTEGER NOT NULL REFERENCES node(id),
    alias TEXT NOT NULL,
    UNIQUE(node_id, alias)
);
CREATE TABLE IF NOT EXISTS source (
    url TEXT PRIMARY KEY,
    title TEXT,
    credibility TEXT,
    retrieved_at TEXT
);
CREATE TABLE IF NOT EXISTS decision (
    id TEXT PRIMARY KEY,
    statement TEXT NOT NULL,
    rationale TEXT,
    alternatives_rejected TEXT,        -- JSON-encoded list
    topic TEXT,
    decided_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS cites (
    node_id INTEGER NOT NULL REFERENCES node(id),
    source_url TEXT NOT NULL REFERENCES source(url),
    note TEXT
);
CREATE TABLE IF NOT EXISTS informed_by (
    decision_id TEXT NOT NULL REFERENCES decision(id),
    source_url TEXT NOT NULL REFERENCES source(url)
);
CREATE TABLE IF NOT EXISTS depends_on (
    decision_id TEXT NOT NULL REFERENCES decision(id),
    node_id INTEGER NOT NULL REFERENCES node(id),
    role TEXT NOT NULL                -- 'chose' | 'rejected'
);
CREATE TABLE IF NOT EXISTS relates_to (
    from_node_id INTEGER NOT NULL REFERENCES node(id),
    to_node_id INTEGER NOT NULL REFERENCES node(id)
);
"""

_FACT_LABELS = ("Library", "API", "Concept")


def _normalize(s: str) -> str:
    """Lowercase, strip, collapse internal whitespace — used for both the
    stored `node_key`/alias matching fields and incoming query topics."""
    return " ".join(s.lower().split())


def _slug(s: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")
    return s[:40] or "decision"


class ResearchGraph:
    def __init__(self, db_path: str | Path | None = None) -> None:
        self.db_path = Path(db_path).expanduser() if db_path else RESEARCH_GRAPH_FILE
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self.db_path))
        self._conn.execute("PRAGMA foreign_keys = ON")
        self._ensure_schema()

    def _ensure_schema(self) -> None:
        self._conn.executescript(_SCHEMA)
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()

    # ── reads ────────────────────────────────────────────────────────────

    def _match_node_ids(self, label: str, q: str) -> set[int]:
        cur = self._conn.execute(
            "SELECT id FROM node WHERE label = ? AND "
            "(node_key = ? OR node_key LIKE '%'||?||'%' OR ? LIKE '%'||node_key||'%')",
            (label, q, q, q),
        )
        ids = {r[0] for r in cur.fetchall()}

        cur = self._conn.execute(
            "SELECT n.id FROM node n JOIN node_alias a ON a.node_id = n.id "
            "WHERE n.label = ? AND "
            "(a.alias = ? OR a.alias LIKE '%'||?||'%' OR ? LIKE '%'||a.alias||'%')",
            (label, q, q, q),
        )
        ids.update(r[0] for r in cur.fetchall())
        return ids

    def _fetch_sources(self, node_id: int) -> list[tuple[str, str]]:
        cur = self._conn.execute(
            "SELECT s.url, s.credibility FROM cites c JOIN source s ON s.url = c.source_url "
            "WHERE c.node_id = ?",
            (node_id,),
        )
        return [(r[0], r[1]) for r in cur.fetchall()]

    def get_context(self, topic: str) -> str:
        q = _normalize(topic)

        library_ids = self._match_node_ids("Library", q)
        api_ids = self._match_node_ids("API", q)
        concept_ids = self._match_node_ids("Concept", q)

        cur = self._conn.execute(
            "SELECT statement, rationale, alternatives_rejected, decided_at FROM decision "
            "WHERE topic = ? OR topic LIKE '%'||?||'%' OR ? LIKE '%'||topic||'%' "
            "ORDER BY decided_at",
            (q, q, q),
        )
        decisions = cur.fetchall()

        if not library_ids and not api_ids and not concept_ids and not decisions:
            return f"No stored research on '{topic}' yet. Dispatch the researcher sub-agent to build it."

        lines = [f"Research context for '{topic}':", ""]

        if library_ids:
            cur = self._conn.execute(
                f"SELECT id, name, summary, verdict, updated_at FROM node "
                f"WHERE id IN ({','.join('?' * len(library_ids))})",
                tuple(library_ids),
            )
            for node_id, name, summary, verdict, updated_at in cur.fetchall():
                lines.append(f"LIBRARY  {name}  [{verdict}]  (updated {_fmt_date(updated_at)})")
                lines.append(f"  {summary}")
                sources = self._fetch_sources(node_id)
                if sources:
                    lines.append("  Sources:")
                    for url, cred in sources:
                        lines.append(f"    - {url}  ({cred})" if cred else f"    - {url}")
                lines.append("")

        if api_ids:
            cur = self._conn.execute(
                f"SELECT id, name, summary, quirks, updated_at FROM node "
                f"WHERE id IN ({','.join('?' * len(api_ids))})",
                tuple(api_ids),
            )
            for node_id, name, summary, quirks, updated_at in cur.fetchall():
                lines.append(f"API  {name}  (updated {_fmt_date(updated_at)})")
                lines.append(f"  {summary}")
                if quirks:
                    lines.append(f"  Quirks: {quirks}")
                sources = self._fetch_sources(node_id)
                if sources:
                    lines.append("  Sources:")
                    for url, cred in sources:
                        lines.append(f"    - {url}  ({cred})" if cred else f"    - {url}")
                lines.append("")

        if concept_ids:
            cur = self._conn.execute(
                f"SELECT name FROM node WHERE id IN ({','.join('?' * len(concept_ids))})",
                tuple(concept_ids),
            )
            names = [r[0] for r in cur.fetchall()]
            lines.append(f"CONCEPTS  {', '.join(names)}")
            lines.append("")

        if decisions:
            lines.append("DECISIONS")
            for statement, rationale, alternatives_json, decided_at in decisions:
                alternatives = json.loads(alternatives_json) if alternatives_json else []
                lines.append(f"  - [{_fmt_date(decided_at)}] {statement}")
                if rationale:
                    lines.append(f"    Why: {rationale}")
                if alternatives:
                    lines.append(f"    Rejected: {', '.join(alternatives)}")
            lines.append("")

        return "\n".join(lines).rstrip()

    # ── writes ───────────────────────────────────────────────────────────

    def record_decision(
        self, decision: str, rationale: str, alternatives_rejected: list[str] | str
    ) -> None:
        if isinstance(alternatives_rejected, str):
            alts = [a.strip() for a in re.split(r"[\n,]+", alternatives_rejected) if a.strip()]
        else:
            alts = [a.strip() for a in alternatives_rejected if a.strip()]

        decided_at = datetime.now(timezone.utc)
        decided_at_iso = decided_at.isoformat()
        decision_id = f"{_slug(decision)}-{sha1((decision + decided_at_iso).encode()).hexdigest()[:8]}"
        topic = _normalize(decision)

        self._conn.execute(
            "INSERT INTO decision (id, statement, rationale, alternatives_rejected, topic, decided_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (decision_id, decision, rationale, json.dumps(alts), topic, decided_at_iso),
        )

        decision_lower = decision.lower()
        for label in _FACT_LABELS:
            try:
                cur = self._conn.execute(
                    "SELECT id FROM node WHERE label = ? AND node_key <> '' AND ? LIKE '%'||node_key||'%'",
                    (label, decision_lower),
                )
                for (node_id,) in cur.fetchall():
                    self._conn.execute(
                        "INSERT INTO depends_on (decision_id, node_id, role) VALUES (?, ?, 'chose')",
                        (decision_id, node_id),
                    )
            except Exception:
                log.warning("record_decision: failed linking chosen %s nodes", label, exc_info=True)

            for alt in alts:
                alt_lower = alt.lower()
                try:
                    cur = self._conn.execute(
                        "SELECT id FROM node WHERE label = ? AND node_key <> '' AND ? LIKE '%'||node_key||'%'",
                        (label, alt_lower),
                    )
                    for (node_id,) in cur.fetchall():
                        self._conn.execute(
                            "INSERT INTO depends_on (decision_id, node_id, role) VALUES (?, ?, 'rejected')",
                            (decision_id, node_id),
                        )
                except Exception:
                    log.warning("record_decision: failed linking rejected %s nodes", label, exc_info=True)

        self._conn.commit()

    def _upsert_source(self, url: str, title: str, credibility: str, now_iso: str) -> None:
        self._conn.execute(
            "INSERT INTO source (url, title, credibility, retrieved_at) VALUES (?, ?, ?, ?) "
            "ON CONFLICT(url) DO UPDATE SET title = excluded.title, "
            "credibility = excluded.credibility, retrieved_at = excluded.retrieved_at",
            (url, title, credibility, now_iso),
        )

    def _upsert_node(self, label: str, node_key: str, name: str, now_iso: str, **extra) -> int:
        cols = {"summary": None, "verdict": None, "quirks": None, **extra}
        self._conn.execute(
            "INSERT INTO node (label, node_key, name, summary, verdict, quirks, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(label, node_key) DO UPDATE SET name = excluded.name, "
            "summary = excluded.summary, verdict = excluded.verdict, quirks = excluded.quirks, "
            "updated_at = excluded.updated_at",
            (label, node_key, name, cols["summary"], cols["verdict"], cols["quirks"], now_iso),
        )
        cur = self._conn.execute(
            "SELECT id FROM node WHERE label = ? AND node_key = ?", (label, node_key)
        )
        return cur.fetchone()[0]

    def _upsert_aliases(self, node_id: int, aliases: list[str]) -> None:
        for alias in aliases:
            self._conn.execute(
                "INSERT OR IGNORE INTO node_alias (node_id, alias) VALUES (?, ?)",
                (node_id, alias),
            )

    def _cite(self, node_id: int, url: str) -> None:
        self._conn.execute(
            "INSERT INTO cites (node_id, source_url, note) VALUES (?, ?, ?)",
            (node_id, url, ""),
        )

    def record_facts(self, facts: "ResearchFacts | dict") -> dict:
        if isinstance(facts, dict):
            facts = ResearchFacts.model_validate(facts)

        now_iso = datetime.now(timezone.utc).isoformat()
        valid_urls = {s.url for s in facts.sources}

        for source in facts.sources:
            self._upsert_source(source.url, source.title, source.credibility, now_iso)

        counts = {"libraries": 0, "apis": 0, "concepts": 0, "sources": len(facts.sources)}

        for lib in facts.libraries:
            key = _normalize(lib.name)
            aliases = [_normalize(a) for a in lib.aliases]
            node_id = self._upsert_node(
                "Library", key, lib.name, now_iso,
                summary=lib.summary, verdict=lib.verdict,
            )
            self._upsert_aliases(node_id, aliases)
            for url in lib.sources:
                if url not in valid_urls:
                    log.warning("record_facts: library %r cites url %r not in top-level sources; skipping", lib.name, url)
                    continue
                self._cite(node_id, url)
            counts["libraries"] += 1

        for api in facts.apis:
            key = _normalize(api.name)
            aliases = [_normalize(a) for a in api.aliases]
            node_id = self._upsert_node(
                "API", key, api.name, now_iso,
                summary=api.summary, quirks=api.quirks,
            )
            self._upsert_aliases(node_id, aliases)
            for url in api.sources:
                if url not in valid_urls:
                    log.warning("record_facts: api %r cites url %r not in top-level sources; skipping", api.name, url)
                    continue
                self._cite(node_id, url)
            counts["apis"] += 1

        for concept in facts.concepts:
            key = _normalize(concept.name)
            aliases = [_normalize(a) for a in concept.aliases]
            node_id = self._upsert_node(
                "Concept", key, concept.name, now_iso,
                summary=concept.summary,
            )
            self._upsert_aliases(node_id, aliases)
            for url in concept.sources:
                if url not in valid_urls:
                    log.warning("record_facts: concept %r cites url %r not in top-level sources; skipping", concept.name, url)
                    continue
                self._cite(node_id, url)
            counts["concepts"] += 1

        self._conn.commit()
        return counts


def _fmt_date(iso_str: str) -> str:
    """Parse an ISO-8601 timestamp (as stored via datetime.isoformat()) and
    format it the same way the old Kuzu TIMESTAMP columns were displayed."""
    return datetime.fromisoformat(iso_str).strftime("%Y-%m-%d")
