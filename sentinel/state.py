from __future__ import annotations

import hashlib
import json
import sqlite3
import time
from pathlib import Path

from .risk import Position


class Store:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.path)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.executescript("""
          CREATE TABLE IF NOT EXISTS kv(key TEXT PRIMARY KEY,value TEXT NOT NULL);
          CREATE TABLE IF NOT EXISTS intents(id TEXT PRIMARY KEY,status TEXT NOT NULL,payload TEXT NOT NULL,response TEXT,created_ms INTEGER NOT NULL);
          CREATE TABLE IF NOT EXISTS events(id INTEGER PRIMARY KEY AUTOINCREMENT, timestamp_ms INTEGER NOT NULL,kind TEXT NOT NULL,payload TEXT NOT NULL);
          CREATE TABLE IF NOT EXISTS fills(id TEXT PRIMARY KEY,timestamp_ms INTEGER NOT NULL,payload TEXT NOT NULL);
        """)
        self.db.commit()

    def get(self, key: str, default=None):
        row = self.db.execute("SELECT value FROM kv WHERE key=?", (key,)).fetchone()
        return json.loads(row[0]) if row else default

    def set(self, key: str, value):
        self.db.execute("INSERT INTO kv VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, json.dumps(value, allow_nan=False)))
        self.db.commit()

    def event(self, kind: str, payload: dict, timestamp_ms: int | None = None):
        t = int(timestamp_ms or time.time() * 1000)
        self.db.execute("INSERT INTO events(timestamp_ms,kind,payload) VALUES(?,?,?)", (t, kind, json.dumps(payload, allow_nan=False)))
        self.db.commit()
        print(json.dumps({"timestamp_ms": t, "event": kind, **payload}, allow_nan=False), flush=True)

    def intent(self, identity: str, payload: dict, now_ms: int) -> str | None:
        key = hashlib.sha256(identity.encode()).hexdigest()
        try:
            self.db.execute("INSERT INTO intents VALUES(?, 'submitted', ?, NULL, ?)", (key, json.dumps(payload, allow_nan=False), now_ms))
            self.db.commit()
        except sqlite3.IntegrityError:
            return None
        return key

    def update_intent(self, key: str, status: str, response: dict | None = None):
        self.db.execute("UPDATE intents SET status=?,response=? WHERE id=?", (status, json.dumps(response, allow_nan=False) if response is not None else None, key))
        self.db.commit()

    def unfinished(self) -> list[dict]:
        return [{"id": r[0], "status": r[1], "payload": json.loads(r[2]), "response": json.loads(r[3]) if r[3] else None, "created_ms": r[4]} for r in self.db.execute("SELECT id,status,payload,response,created_ms FROM intents WHERE status IN ('submitted','acknowledged','unknown') ORDER BY created_ms")]

    def positions(self) -> list[Position]:
        return [Position(**p) for p in self.get("positions", [])]

    def complete(self, key: str, positions: list[Position], fill: dict, fill_id: str, timestamp_ms: int):
        # Position update, filled-order journal and intent completion form ONE crash-safe commit.
        with self.db:
            self.db.execute("INSERT INTO kv VALUES('positions',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (json.dumps([p.to_dict() for p in positions], allow_nan=False),))
            self.db.execute("INSERT OR IGNORE INTO fills VALUES(?,?,?)", (fill_id, timestamp_ms, json.dumps(fill, allow_nan=False)))
            self.db.execute("UPDATE intents SET status='done' WHERE id=?", (key,))

    def fills(self) -> list[dict]:
        return [{"id": r[0], "timestamp_ms": r[1], **json.loads(r[2])} for r in self.db.execute("SELECT id,timestamp_ms,payload FROM fills ORDER BY timestamp_ms")]

    def close(self):
        self.db.close()
