"""SQLite state: pipeline runs, proposals and the decisions made on them.

Idempotency lives here. Every proposal has a deterministic `key` built from
what it would do (action + target + intended values). The pipeline inserts a
proposal only if that key has never been seen, so anything already approved,
rejected or applied is never re-proposed. If the world changes (the website
shows a different address, say) the intended values change, the key changes,
and a fresh proposal appears — which is exactly what a reviewer would want.
"""
from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from . import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    locations INTEGER,
    accounts INTEGER,
    new_proposals INTEGER,
    skipped_decided INTEGER,
    superseded INTEGER,
    report TEXT
);
CREATE TABLE IF NOT EXISTS proposals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    key TEXT UNIQUE NOT NULL,
    run_id INTEGER NOT NULL,
    kind TEXT NOT NULL,
    title TEXT NOT NULL,
    account_id TEXT,
    location_slug TEXT,
    confidence TEXT,
    payload TEXT NOT NULL,     -- JSON: the exact API operations + preconditions
    evidence TEXT NOT NULL,    -- JSON: what the reviewer sees
    status TEXT NOT NULL DEFAULT 'pending',  -- pending|approved|applied|rejected|failed|superseded
    created_at TEXT NOT NULL,
    decided_at TEXT,
    decided_by TEXT,
    decision_comment TEXT,
    result TEXT                -- JSON: API responses / error
);
CREATE INDEX IF NOT EXISTS ix_prop_status ON proposals(status);
"""


def now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class Store:
    def __init__(self, path: Path | str | None = None):
        self.path = Path(path or config.DB_PATH)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.conn() as c:
            c.executescript(SCHEMA)

    @contextmanager
    def conn(self):
        c = sqlite3.connect(self.path)
        c.row_factory = sqlite3.Row
        try:
            yield c
            c.commit()
        finally:
            c.close()

    # -- runs --------------------------------------------------------------
    def start_run(self) -> int:
        with self.conn() as c:
            return c.execute("INSERT INTO runs(started_at) VALUES (?)", (now(),)).lastrowid

    def finish_run(self, run_id: int, **stats):
        report = stats.pop("report", None)
        with self.conn() as c:
            c.execute(
                "UPDATE runs SET finished_at=?, locations=?, accounts=?, new_proposals=?, skipped_decided=?, superseded=?, report=? WHERE id=?",
                (now(), stats.get("locations"), stats.get("accounts"), stats.get("new_proposals"),
                 stats.get("skipped_decided"), stats.get("superseded"), json.dumps(report), run_id),
            )

    def last_run(self) -> dict | None:
        with self.conn() as c:
            r = c.execute("SELECT * FROM runs WHERE finished_at IS NOT NULL ORDER BY id DESC LIMIT 1").fetchone()
        if not r:
            return None
        d = dict(r)
        d["report"] = json.loads(d["report"] or "null")
        return d

    # -- proposals ---------------------------------------------------------
    def existing(self, key: str) -> dict | None:
        with self.conn() as c:
            r = c.execute("SELECT * FROM proposals WHERE key=?", (key,)).fetchone()
        return dict(r) if r else None

    def add(self, run_id: int, p: dict) -> bool:
        """Insert unless the key already exists. Returns True if inserted."""
        with self.conn() as c:
            cur = c.execute(
                """INSERT OR IGNORE INTO proposals
                   (key, run_id, kind, title, account_id, location_slug, confidence, payload, evidence, created_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?)""",
                (p["key"], run_id, p["kind"], p["title"], p.get("account_id"), p.get("location_slug"),
                 p.get("confidence"), json.dumps(p["payload"]), json.dumps(p["evidence"]), now()),
            )
            return cur.rowcount == 1

    def supersede_missing(self, live_keys: set[str]) -> int:
        """Pending proposals the latest run no longer generates are stale."""
        with self.conn() as c:
            rows = c.execute("SELECT id, key FROM proposals WHERE status='pending'").fetchall()
            stale = [r["id"] for r in rows if r["key"] not in live_keys]
            for pid in stale:
                c.execute("UPDATE proposals SET status='superseded', decided_at=? WHERE id=?", (now(), pid))
        return len(stale)

    def list(self, status: str | None = None) -> list[dict]:
        q, args = "SELECT * FROM proposals", ()
        if status:
            q, args = q + " WHERE status=?", (status,)
        with self.conn() as c:
            rows = c.execute(q + " ORDER BY id", args).fetchall()
        return [self._hydrate(r) for r in rows]

    def get(self, pid: int) -> dict | None:
        with self.conn() as c:
            r = c.execute("SELECT * FROM proposals WHERE id=?", (pid,)).fetchone()
        return self._hydrate(r) if r else None

    def set_status(self, pid: int, status: str, *, by: str | None = None, comment: str | None = None, result=None):
        with self.conn() as c:
            c.execute(
                "UPDATE proposals SET status=?, decided_at=COALESCE(decided_at, ?), decided_by=COALESCE(?, decided_by),"
                " decision_comment=COALESCE(?, decision_comment), result=COALESCE(?, result) WHERE id=?",
                (status, now(), by, comment, json.dumps(result) if result is not None else None, pid),
            )

    def save_result(self, pid: int, result):
        with self.conn() as c:
            c.execute("UPDATE proposals SET result=? WHERE id=?", (json.dumps(result), pid))

    def counts(self) -> dict:
        with self.conn() as c:
            rows = c.execute("SELECT status, COUNT(*) n FROM proposals GROUP BY status").fetchall()
        return {r["status"]: r["n"] for r in rows}

    @staticmethod
    def _hydrate(r) -> dict:
        d = dict(r)
        for k in ("payload", "evidence", "result"):
            d[k] = json.loads(d[k]) if d.get(k) else None
        return d
