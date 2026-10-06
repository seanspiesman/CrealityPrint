from __future__ import annotations

import json
import sqlite3
import threading
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

from .models import ServiceError


class Store:
    def __init__(self, home: Path):
        self.db = sqlite3.connect(home / "jobs.sqlite3", check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.lock = threading.RLock()
        self.depth = 0
        self.db.executescript('''
            PRAGMA journal_mode=WAL;
            CREATE TABLE IF NOT EXISTS jobs(id TEXT PRIMARY KEY, state TEXT NOT NULL,
                data TEXT NOT NULL, created REAL NOT NULL, updated REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS events(seq INTEGER PRIMARY KEY AUTOINCREMENT,
                job_id TEXT, kind TEXT NOT NULL, data TEXT NOT NULL, time REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS requests(key TEXT PRIMARY KEY, fingerprint TEXT NOT NULL,
                response TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS decisions(job_id TEXT NOT NULL, action TEXT NOT NULL,
                PRIMARY KEY(job_id,action));
        ''')
        self.db.commit()

    @contextmanager
    def transaction(self):
        with self.lock:
            outer = self.depth == 0
            if outer:
                self.db.execute("BEGIN IMMEDIATE")
            self.depth += 1
            try:
                yield
            except BaseException:
                if outer:
                    self.db.rollback()
                raise
            else:
                if outer:
                    self.db.commit()
            finally:
                self.depth -= 1

    def create(self, data: dict) -> dict:
        with self.transaction():
            id = uuid.uuid4().hex
            now = time.time()
            self.db.execute("INSERT INTO jobs VALUES(?,?,?,?,?)", (id, "created", json.dumps(data), now, now))
        self.event(id, "created", {})
        return self.get(id)

    def get(self, id: str) -> dict:
        with self.lock:
            row = self.db.execute("SELECT * FROM jobs WHERE id=?", (id,)).fetchone()
        if row is None:
            raise ServiceError("Unknown job", 404)
        return {"id": row["id"], "state": row["state"], "created": row["created"],
                "updated": row["updated"], **json.loads(row["data"])}

    def list(self) -> list[dict]:
        with self.lock:
            ids = self.db.execute("SELECT id FROM jobs ORDER BY created DESC LIMIT 500").fetchall()
        return [self.get(row[0]) for row in ids]

    def worklist(self) -> list[dict]:
        with self.lock:
            rows = self.db.execute("SELECT id FROM jobs WHERE state IN "
                "('queued','starting','printing','paused','preparing','pausing','resuming','canceling','acquiring') "
                "OR (state='held' AND (json_extract(data,'$.ambiguous_start')=1 "
                "OR json_extract(data,'$.control_reconciliation_required')=1)) ORDER BY created").fetchall()
        return [self.get(row[0]) for row in rows]

    def update(self, id: str, state: str | None = None, **changes) -> dict:
        with self.transaction():
            job = self.get(id)
            data = {k: v for k, v in job.items() if k not in {"id", "state", "created", "updated"}}
            data.update(changes)
            self.db.execute("UPDATE jobs SET state=?,data=?,updated=? WHERE id=?",
                            (state or job["state"], json.dumps(data), time.time(), id))
        self.event(id, state or "updated", changes)
        return self.get(id)

    def event(self, id: str | None, kind: str, data: dict):
        with self.transaction():
            self.db.execute("INSERT INTO events(job_id,kind,data,time) VALUES(?,?,?,?)",
                            (id, kind, json.dumps(data), time.time()))

    def events(self, after: int = 0, job_id: str | None = None) -> list[dict]:
        with self.lock:
            rows = self.db.execute("SELECT * FROM events WHERE seq>? AND (? IS NULL OR job_id=?) "
                                   "ORDER BY seq LIMIT 200", (after, job_id, job_id)).fetchall()
        return [{**dict(r), "data": json.loads(r["data"])} for r in rows]

    def remembered(self, key: str, fingerprint: str) -> dict | None:
        with self.lock:
            row = self.db.execute("SELECT fingerprint,response FROM requests WHERE key=?", (key,)).fetchone()
        if row:
            if row["fingerprint"] != fingerprint:
                raise ServiceError("Idempotency key was reused with different input")
            return json.loads(row["response"])
        return None

    def remember(self, key: str, fingerprint: str, response: dict):
        with self.transaction():
            self.db.execute("INSERT INTO requests VALUES(?,?,?)", (key, fingerprint, json.dumps(response)))

    def approve(self, id: str, action: str):
        self.get(id)
        with self.transaction():
            self.db.execute("INSERT OR IGNORE INTO decisions VALUES(?,?)", (id, action))
        self.event(id, "owner_decision", {"action": action})

    def consume(self, id: str, action: str) -> bool:
        with self.transaction():
            result = self.db.execute("DELETE FROM decisions WHERE job_id=? AND action=?", (id, action))
            return result.rowcount == 1

    def close(self):
        self.db.close()
