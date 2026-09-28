import logging
import json
import os
from datetime import datetime, timezone
from typing import Any, Optional

from app.services.storage_service import SQLiteStore, store

logger = logging.getLogger("aftermath.memory")


class HindsightService:
    """Explicit live-Hindsight or local-development memory adapter; never silently switches modes."""

    def __init__(self, database: SQLiteStore = store, mode: Optional[str] = None, client: Any = None):
        self.database = database
        self.mode = (mode or os.getenv("AFTERMATH_MEMORY_MODE", "local")).strip().lower()
        self.bank_id = os.getenv("HINDSIGHT_BANK_ID", "aftermath-incidents")
        self.base_url = os.getenv("HINDSIGHT_BASE_URL", "http://localhost:8888")
        self.client = client
        self.last_error: Optional[str] = None
        self.version: Optional[str] = None
        if self.mode not in {"local", "hindsight"}:
            self.last_error = "AFTERMATH_MEMORY_MODE must be 'local' or 'hindsight'."
        elif self.mode == "hindsight" and self.client is None:
            self._connect()

    def _connect(self):
        try:
            from hindsight_client import Hindsight
            self.client = Hindsight(base_url=self.base_url, api_key=os.getenv("HINDSIGHT_API_KEY") or None, timeout=5)
            response = self.client.get_version()
            self.version = getattr(response, "version", None) or str(response)
            self.client.get_bank_config(self.bank_id)
            self.last_error = None
        except Exception as exc:
            self.client = None
            self.last_error = "Hindsight is unavailable or could not be reached."
            logger.warning("Hindsight connection check failed: %s", exc)

    def runtime_status(self) -> dict:
        if self.mode == "local":
            return {"mode": "LOCAL_DEVELOPMENT", "connected": False, "available": self.database.health(), "bank_id": self.bank_id, "message": "Using the explicitly selected SQLite development memory store."}
        if self.client is not None and not self.last_error:
            return {"mode": "LIVE_MEMORY", "connected": True, "available": True, "bank_id": self.bank_id, "version": self.version, "message": "Connected to Hindsight."}
        return {"mode": "MEMORY_UNAVAILABLE", "connected": False, "available": False, "bank_id": self.bank_id, "message": self.last_error or "Hindsight is unavailable."}

    def _unavailable(self):
        if self.mode == "hindsight":
            self.last_error = "Hindsight request failed; no local fallback was used."
            logger.error("Hindsight request failed; refusing silent local fallback.")

    @staticmethod
    def _score(result: Any) -> Optional[float]:
        scores = getattr(result, "scores", None)
        value = getattr(scores, "final", None) if scores else None
        return float(value) if isinstance(value, (int, float)) else None

    def recall(self, query: str, organization_id: str, incident: dict, limit: int = 10) -> list[dict]:
        if self.mode == "local":
            return self.database.list_memories(organization_id)[:limit]
        if self.client is None:
            return []
        try:
            response = self.client.recall(bank_id=self.bank_id, query=query, tags=[f"org:{organization_id}"], tags_match="all_strict", budget="low")
            normalized = []
            for item in (getattr(response, "results", None) or []):
                metadata = getattr(item, "metadata", None) or {}
                # Tenant tags are a mandatory scope, and metadata is rechecked before use.
                if metadata.get("organization_id") != organization_id:
                    continue
                normalized.append({
                    "id": getattr(item, "document_id", None) or getattr(item, "id", "unknown"),
                    "incident_id": metadata.get("incident_id"),
                    "content": getattr(item, "text", ""),
                    "context": getattr(item, "context", None),
                    "metadata": metadata,
                    "tags": getattr(item, "tags", None) or [],
                    "score": self._score(item),
                    "source": "hindsight",
                })
            self.last_error = None
            return normalized[:limit]
        except Exception as exc:
            self._unavailable()
            logger.exception("Hindsight recall failed: %s", exc)
            return []

    def retain(self, organization_id: str, incident: dict) -> dict:
        memory = {
            "id": incident["id"],
            "incident_id": incident["id"],
            "content": self._memory_content(incident),
            "context": incident.get("context") or incident.get("recent_changes") or "Resolved incident",
            "metadata": {
                "organization_id": organization_id,
                "incident_id": incident["id"],
                "service": incident["service"],
                "environment": incident["environment"],
                "error_message": incident["error_message"],
                "symptoms": incident["symptoms"],
                "recent_changes": incident.get("recent_changes") or "",
                "dependencies": ", ".join(incident.get("dependencies") or []),
                "root_cause": incident.get("root_cause") or "",
                "successful_fix": incident.get("successful_action") or "",
                "title": incident.get("title") or "",
                "actions_json": json.dumps(incident.get("actions") or incident.get("attempts") or [], ensure_ascii=False),
                "lessons_json": json.dumps(incident.get("lessons_learned") or [], ensure_ascii=False),
            },
            "actions": incident.get("actions") or incident.get("attempts") or [],
            "title": incident.get("title") or "",
            "lessons_learned": incident.get("lessons_learned") or [],
            "tags": [f"org:{organization_id}", f"service:{incident['service'].casefold()}", f"error:{incident['error_message'].casefold()}"],
            "timestamp": incident.get("resolved_at") or datetime.now(timezone.utc).isoformat(),
        }
        if self.mode == "local":
            self.database.save_memory(organization_id, memory)
            return {"saved": True, "mode": "LOCAL_DEVELOPMENT", "memory_id": incident["id"]}
        if self.client is None:
            return {"saved": False, "mode": "MEMORY_UNAVAILABLE", "error": self.last_error or "Hindsight is not connected."}
        try:
            result = self.client.retain(
                bank_id=self.bank_id,
                content=memory["content"],
                context=memory["context"],
                metadata={key: str(value) for key, value in memory["metadata"].items()},
                tags=memory["tags"],
                document_id=f"{organization_id}:{incident['id']}",
                update_mode="replace",
            )
            if not getattr(result, "success", False):
                self.last_error = "Hindsight did not confirm the memory write."
                return {"saved": False, "mode": "LIVE_MEMORY", "error": self.last_error}
            # A local projection is for listing only; live retrieval remains Hindsight-only.
            memory["id"] = incident["id"]
            memory["source"] = "hindsight"
            self.database.save_memory(organization_id, memory)
            self.last_error = None
            return {"saved": True, "mode": "LIVE_MEMORY", "memory_id": incident["id"]}
        except Exception as exc:
            self._unavailable()
            logger.exception("Hindsight retain failed: %s", exc)
            return {"saved": False, "mode": "MEMORY_UNAVAILABLE", "error": self.last_error}

    @staticmethod
    def _memory_content(incident: dict) -> str:
        actions = incident.get("actions") or incident.get("attempts") or []
        action_lines = [
            f"- {item.get('timestamp')}: {item.get('action')} — {item.get('result')} (status: {item.get('status', 'COMPLETED')}; evidence: {item.get('evidence') or 'not provided'}; notes: {item.get('notes') or item.get('reason_or_why') or 'not provided'})"
            for item in actions
        ]
        lesson_lines = [f"- {lesson}" for lesson in incident.get("lessons_learned") or []]
        return "\n".join([
            f"Incident ID: {incident['id']}", f"Title: {incident['title']}",
            f"Service: {incident['service']}", f"Environment: {incident['environment']}",
            f"Severity: {incident.get('severity') or 'UNSPECIFIED'}", f"Impact: {incident.get('impact') or 'Not recorded'}",
            f"Error: {incident['error_message']}", f"Symptoms: {incident['symptoms']}",
            f"Recent changes: {incident.get('recent_changes') or 'None recorded'}",
            f"Dependencies: {', '.join(incident.get('dependencies') or []) or 'None recorded'}",
            f"Context: {incident.get('context') or 'None recorded'}", "Actions and outcomes:",
            *(action_lines or ["- No investigation actions recorded"]),
            f"Root cause: {incident.get('root_cause') or 'Not confirmed'}",
            f"Successful resolution: {incident.get('successful_action') or 'Not recorded'}", "Lessons:",
            *(lesson_lines or ["- No lesson recorded"]),
        ])


hindsight_service = HindsightService()
