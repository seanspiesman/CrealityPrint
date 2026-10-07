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
            CREATE TABLE IF NOT EXISTS questions(id TEXT PRIMARY KEY, job_id TEXT, question TEXT NOT NULL,
                status TEXT NOT NULL, answer TEXT, created REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS conversations(id TEXT PRIMARY KEY, created REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS messages(seq INTEGER PRIMARY KEY AUTOINCREMENT,
                conversation_id TEXT NOT NULL, data TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS alerts(id INTEGER PRIMARY KEY, job_id TEXT, kind TEXT NOT NULL,
                message TEXT NOT NULL, acknowledged INTEGER DEFAULT 0, delivered INTEGER DEFAULT 0);
            CREATE TABLE IF NOT EXISTS cursors(name TEXT PRIMARY KEY, value INTEGER NOT NULL);
            CREATE TABLE IF NOT EXISTS budget_grants(job_id TEXT PRIMARY KEY, fingerprint TEXT NOT NULL);
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

    def has_decision(self, id: str, action: str) -> bool:
        with self.lock:
            return self.db.execute("SELECT 1 FROM decisions WHERE job_id=? AND action=?", (id, action)).fetchone() is not None

    def consume(self, id: str, action: str) -> bool:
        with self.transaction():
            result = self.db.execute("DELETE FROM decisions WHERE job_id=? AND action=?", (id, action))
            return result.rowcount == 1

    def budget_approve(self, id: str, fingerprint: str):
        self.get(id)
        with self.transaction():
            self.db.execute("INSERT OR REPLACE INTO budget_grants VALUES(?,?)", (id, fingerprint))
        self.event(id, "owner_decision", {"action": "budget"})

    def budget_allowed(self, id: str, fingerprint: str) -> bool:
        with self.lock:
            return self.db.execute("SELECT 1 FROM budget_grants WHERE job_id=? AND fingerprint=?",
                                   (id, fingerprint)).fetchone() is not None

    def consume_budget(self, id: str):
        with self.transaction():
            self.db.execute("DELETE FROM budget_grants WHERE job_id=?", (id,))

    def request_question(self, question: str, job_id: str | None = None) -> dict:
        if job_id is not None:
            self.get(job_id)
        with self.transaction():
            id = uuid.uuid4().hex
            self.db.execute("INSERT INTO questions VALUES(?,?,?,'open',NULL,?)",
                            (id, job_id, question, time.time()))
            self.event(job_id, "question_requested", {"question_id": id})
        return self.question(id)

    def question(self, id: str) -> dict:
        with self.lock:
            row = self.db.execute("SELECT * FROM questions WHERE id=?", (id,)).fetchone()
        if row is None:
            raise ServiceError("Unknown question", 404)
        return dict(row)

    def questions(self) -> list[dict]:
        with self.lock:
            return [dict(r) for r in self.db.execute("SELECT * FROM questions ORDER BY created DESC LIMIT 200")]

    def has_open_question(self, job_id: str) -> bool:
        with self.lock:
            return self.db.execute("SELECT 1 FROM questions WHERE job_id=? AND status='open' LIMIT 1",
                                   (job_id,)).fetchone() is not None

    def answer_question(self, id: str, answer: str) -> dict:
        with self.transaction():
            question = self.question(id)
            if question["status"] == "answered" and question["answer"] == answer:
                return question
            if question["status"] != "open":
                raise ServiceError("Question already answered")
            self.db.execute("UPDATE questions SET status='answered',answer=? WHERE id=?", (answer, id))
            self.event(question["job_id"], "question_answered", {"question_id": id})
        # An answer records owner input; it never grants a budget, resume, or qualification.
        return self.question(id)

    def conversation(self, id: str | None = None) -> dict:
        with self.transaction():
            if id is None:
                id = uuid.uuid4().hex
                self.db.execute("INSERT INTO conversations VALUES(?,?)", (id, time.time()))
            if not self.db.execute("SELECT 1 FROM conversations WHERE id=?", (id,)).fetchone():
                raise ServiceError("Unknown conversation", 404)
            rows = self.db.execute("SELECT data FROM messages WHERE conversation_id=? ORDER BY seq", (id,)).fetchall()
            return {"id": id, "messages": [json.loads(r[0]) for r in rows]}

    def conversations(self):
        with self.lock:
            return [dict(r) for r in self.db.execute("SELECT * FROM conversations ORDER BY created DESC LIMIT 50")]

    def message(self, id: str, data: dict):
        with self.transaction():
            self.conversation(id)
            self.db.execute("INSERT INTO messages(conversation_id,data) VALUES(?,?)", (id, json.dumps(data)))

    def alerts(self):
        with self.lock:
            return [dict(r) for r in self.db.execute("SELECT * FROM alerts ORDER BY id DESC LIMIT 200")]

    def acknowledge(self, id: int):
        with self.transaction():
            self.db.execute("UPDATE alerts SET acknowledged=1 WHERE id=?", (id,))

    def pending_notifications(self):
        with self.lock:
            return [dict(r) for r in self.db.execute("SELECT * FROM alerts WHERE delivered=0 ORDER BY id LIMIT 20")]

    def mark_delivered(self, id: int):
        with self.transaction():
            self.db.execute("UPDATE alerts SET delivered=1 WHERE id=?", (id,))

    def collect_alerts(self):
        messages = {"completed": "Print completed", "failed": "Print failed", "held": "Job needs your decision",
                    "failure_detected": "Detected print failure; pause requested",
                    "monitoring_unavailable": "Monitoring lost; print continues and new starts are held",
                    "monitoring_recovered": "Print monitoring recovered", "recovery": "Service recovery completed",
                    "question_requested": "The agent needs your input"}
        with self.transaction():
            row = self.db.execute("SELECT value FROM cursors WHERE name='alerts'").fetchone()
            after = row[0] if row else 0
            for event in self.events(after):
                if event["kind"] in messages:
                    self.db.execute("INSERT OR IGNORE INTO alerts(id,job_id,kind,message) VALUES(?,?,?,?)",
                        (event["seq"], event["job_id"], event["kind"], messages[event["kind"]]))
                after = event["seq"]
            self.db.execute("INSERT OR REPLACE INTO cursors VALUES('alerts',?)", (after,))

    def close(self):
        self.db.close()
