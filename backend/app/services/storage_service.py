import json
import os
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import List, Optional

from app.schemas.incident import IncidentRecord, TimelineEvent


class SQLiteStore:
    """Small SQLite persistence layer; every operation uses its own safe transaction."""

    def __init__(self, database_path: Optional[str] = None):
        configured = database_path or os.getenv("AFTERMATH_DATABASE_PATH")
        self.database_path = configured or str(Path(__file__).resolve().parents[2] / "data" / "aftermath.sqlite3")
        if self.database_path != ":memory:":
            Path(self.database_path).parent.mkdir(parents=True, exist_ok=True)
        self.initialize()

    def connect(self):
        connection = sqlite3.connect(self.database_path, timeout=15, isolation_level="DEFERRED")
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 15000")
        return connection

    @contextmanager
    def connection(self):
        db = self.connect()
        try:
            yield db
            db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    def initialize(self):
        with self.connection() as db:
            db.execute("PRAGMA journal_mode = WAL")
            db.executescript("""
                CREATE TABLE IF NOT EXISTS incidents (
                    organization_id TEXT NOT NULL,
                    id TEXT NOT NULL,
                    data TEXT NOT NULL,
                    PRIMARY KEY (organization_id, id)
                );
                CREATE TABLE IF NOT EXISTS timeline_events (
                    organization_id TEXT NOT NULL,
                    id TEXT NOT NULL,
                    incident_id TEXT NOT NULL,
                    timestamp TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    data TEXT NOT NULL,
                    PRIMARY KEY (organization_id, id),
                    FOREIGN KEY (organization_id, incident_id)
                        REFERENCES incidents (organization_id, id) ON DELETE CASCADE
                );
                CREATE INDEX IF NOT EXISTS timeline_incident_idx
                    ON timeline_events (organization_id, incident_id, timestamp);
                CREATE TABLE IF NOT EXISTS memories (
                    organization_id TEXT NOT NULL,
                    id TEXT NOT NULL,
                    data TEXT NOT NULL,
                    PRIMARY KEY (organization_id, id)
                );
                CREATE TABLE IF NOT EXISTS sequences (
                    organization_id TEXT PRIMARY KEY,
                    last_id INTEGER NOT NULL DEFAULT 0
                );
            """)

    def next_incident_id(self, organization_id: str) -> str:
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT last_id FROM sequences WHERE organization_id=?", (organization_id,)).fetchone()
            last_id = row[0] if row else 0
            if row is None:
                existing = db.execute("SELECT COUNT(*) FROM incidents WHERE organization_id=?", (organization_id,)).fetchone()[0]
                last_id = existing
            next_id = last_id + 1
            db.execute("INSERT INTO sequences(organization_id,last_id) VALUES(?,?) ON CONFLICT(organization_id) DO UPDATE SET last_id=excluded.last_id", (organization_id, next_id))
            return f"INC-{next_id:06d}"

    def save_incident(self, incident: IncidentRecord):
        with self.connection() as db:
            db.execute("INSERT INTO incidents(organization_id,id,data) VALUES(?,?,?) ON CONFLICT(organization_id,id) DO UPDATE SET data=excluded.data", (incident.organization_id, incident.id, incident.model_dump_json()))

    def save_incident_with_events(self, incident: IncidentRecord, events: List[TimelineEvent]):
        with self.connection() as db:
            db.execute("INSERT INTO incidents(organization_id,id,data) VALUES(?,?,?) ON CONFLICT(organization_id,id) DO UPDATE SET data=excluded.data", (incident.organization_id, incident.id, incident.model_dump_json()))
            for event in events:
                db.execute("INSERT INTO timeline_events(organization_id,id,incident_id,timestamp,event_type,data) VALUES(?,?,?,?,?,?)", (incident.organization_id, event.id, event.incident_id, event.timestamp.isoformat(), event.event_type, event.model_dump_json()))

    def get_incident(self, organization_id: str, incident_id: str) -> Optional[IncidentRecord]:
        with self.connection() as db:
            row = db.execute("SELECT data FROM incidents WHERE organization_id=? AND id=?", (organization_id, incident_id)).fetchone()
        return IncidentRecord.model_validate_json(row["data"]) if row else None

    def list_incidents(self, organization_id: str) -> List[IncidentRecord]:
        with self.connection() as db:
            rows = db.execute("SELECT data FROM incidents WHERE organization_id=?", (organization_id,)).fetchall()
        records = [IncidentRecord.model_validate_json(row["data"]) for row in rows]
        return sorted(records, key=lambda record: record.created_at, reverse=True)

    def add_event(self, organization_id: str, event: TimelineEvent):
        with self.connection() as db:
            db.execute("INSERT INTO timeline_events(organization_id,id,incident_id,timestamp,event_type,data) VALUES(?,?,?,?,?,?)", (organization_id, event.id, event.incident_id, event.timestamp.isoformat(), event.event_type, event.model_dump_json()))

    def list_events(self, organization_id: str, incident_id: str) -> List[TimelineEvent]:
        with self.connection() as db:
            rows = db.execute("SELECT data FROM timeline_events WHERE organization_id=? AND incident_id=? ORDER BY timestamp,id", (organization_id, incident_id)).fetchall()
        return [TimelineEvent.model_validate_json(row["data"]) for row in rows]

    def save_memory(self, organization_id: str, memory: dict):
        with self.connection() as db:
            db.execute("INSERT INTO memories(organization_id,id,data) VALUES(?,?,?) ON CONFLICT(organization_id,id) DO UPDATE SET data=excluded.data", (organization_id, memory["id"], json.dumps(memory, ensure_ascii=False)))

    def list_memories(self, organization_id: str) -> List[dict]:
        with self.connection() as db:
            rows = db.execute("SELECT data FROM memories WHERE organization_id=? ORDER BY id", (organization_id,)).fetchall()
        memories = [json.loads(row["data"]) for row in rows]
        return sorted(memories, key=lambda item: item.get("timestamp") or "", reverse=True)

    def health(self) -> bool:
        try:
            with self.connection() as db:
                db.execute("SELECT 1").fetchone()
            return True
        except sqlite3.Error:
            return False


store = SQLiteStore()
