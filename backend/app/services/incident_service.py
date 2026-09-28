import json
import logging
import uuid
from datetime import datetime, timezone
from typing import List, Optional

from app.schemas.incident import IncidentAnalysisResult, IncidentCreate, IncidentRecord, IncidentResolutionUpdate, InvestigationAction, TimelineEvent
from app.services.hindsight_service import HindsightService, hindsight_service
from app.services.reasoning_service import ContextComparisonEngine, reasoning_engine
from app.services.storage_service import SQLiteStore, store

logger = logging.getLogger("aftermath.incident_service")


class IncidentService:
    def __init__(self, database: SQLiteStore = store, memory: HindsightService = hindsight_service, engine: ContextComparisonEngine = reasoning_engine):
        self.database = database
        self.memory = memory
        self.engine = engine

    def _event(self, incident: IncidentRecord, event_type: str, message: str, details: Optional[dict] = None) -> TimelineEvent:
        return TimelineEvent(
            id=str(uuid.uuid4()), incident_id=incident.id, timestamp=datetime.now(timezone.utc),
            event_type=event_type, message=message, details=details or {},
        )

    def create_incident(self, payload: IncidentCreate, organization_id: str) -> IncidentRecord:
        incident = IncidentRecord(id=self.database.next_incident_id(organization_id), organization_id=organization_id, **payload.model_dump())
        self.database.save_incident_with_events(incident, [self._event(incident, "INCIDENT_CREATED", "Incident created.", {"service": incident.service, "environment": incident.environment, "severity": incident.severity})])
        return incident

    def get_incident(self, incident_id: str, organization_id: str) -> Optional[IncidentRecord]:
        return self.database.get_incident(organization_id, incident_id)

    def list_incidents(self, organization_id: str) -> List[IncidentRecord]:
        return self.database.list_incidents(organization_id)

    def list_events(self, incident_id: str, organization_id: str) -> List[TimelineEvent]:
        if not self.get_incident(incident_id, organization_id):
            raise ValueError("Incident not found")
        return self.database.list_events(organization_id, incident_id)

    def record_action(self, incident_id: str, action: InvestigationAction, organization_id: str) -> IncidentRecord:
        incident = self.get_incident(incident_id, organization_id)
        if not incident:
            raise ValueError("Incident not found")
        if incident.status == "RESOLVED":
            raise ValueError("Cannot add an action to a resolved incident")
        incident.actions.append(action)
        self.database.save_incident_with_events(incident, [self._event(incident, "ACTION_RECORDED", "Investigation action recorded.", action.model_dump(mode="json"))])
        return incident

    def analyze_incident(self, incident_id: str, organization_id: str) -> IncidentAnalysisResult:
        incident = self.get_incident(incident_id, organization_id)
        if not incident:
            raise ValueError("Incident not found")
        self.database.add_event(organization_id, self._event(incident, "ANALYSIS_STARTED", "Evidence comparison started."))
        try:
            analysis = self.engine.analyze_incident(incident)
            incident.analysis = analysis
            events = [
                self._event(incident, "MEMORY_RETRIEVAL", "Historical memory search completed.", {
                    "mode": self.memory.runtime_status()["mode"],
                    "relevant_experience_count": len(analysis.historical_evidence),
                }),
                self._event(incident, "ANALYSIS_COMPLETED", "Evidence comparison completed.", {"recommendation_state": analysis.recommendation_type.value}),
            ]
            self.database.save_incident_with_events(incident, events)
            return analysis
        except Exception:
            self.database.add_event(organization_id, self._event(incident, "ANALYSIS_FAILED", "Evidence comparison failed."))
            logger.exception("Incident analysis failed for %s", incident.id)
            raise

    def resolve_incident(self, incident_id: str, resolution: IncidentResolutionUpdate, organization_id: str) -> IncidentRecord:
        incident = self.get_incident(incident_id, organization_id)
        if not incident:
            raise ValueError("Incident not found")
        if incident.status == "RESOLVED" and incident.learning_saved:
            return incident
        if incident.status != "RESOLVED":
            new_actions = resolution.all_actions()
            incident.actions.extend(action for action in new_actions if action.model_dump(mode="json") not in [item.model_dump(mode="json") for item in incident.actions])
            incident.attempts = incident.actions
            incident.status = "RESOLVED"
            incident.resolved_at = datetime.now(timezone.utc)
            incident.successful_action = resolution.successful_action
            incident.root_cause = resolution.root_cause
            incident.lessons_learned = resolution.lessons_learned
            incident.learning_saved = False
            incident.learning_error = None
            self.database.save_incident_with_events(incident, [self._event(incident, "INCIDENT_RESOLVED", "Incident resolution recorded.", {"root_cause": incident.root_cause, "successful_action": incident.successful_action})])

        try:
            result = self.memory.retain(organization_id, incident.model_dump(mode="json"))
        except Exception as exc:
            logger.exception("Memory retention failed for incident %s", incident.id)
            result = {"saved": False, "error": "Memory retention failed."}
        incident.learning_saved = bool(result.get("saved"))
        incident.learning_error = None if incident.learning_saved else result.get("error", "Learning was not saved.")
        self.database.save_incident_with_events(incident, [self._event(
            incident, "LEARNING_RETAINED" if incident.learning_saved else "LEARNING_RETENTION_FAILED",
            "Incident experience saved to the selected memory store." if incident.learning_saved else "Incident resolved, but learning could not be saved.",
            {"mode": result.get("mode"), "saved": incident.learning_saved, "memory_id": result.get("memory_id")},
        )])
        return incident


incident_service = IncidentService()
