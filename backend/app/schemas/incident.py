from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class RecommendationType(str, Enum):
    REUSE = "REUSE"
    CAUTION = "CAUTION"
    AVOID = "AVOID"
    UNKNOWN = "UNKNOWN"


class ActionResult(str, Enum):
    SUCCESS = "SUCCESS"
    FAILED = "FAILED"
    PARTIAL = "PARTIAL"
    INCONCLUSIVE = "INCONCLUSIVE"


class ActionStatus(str, Enum):
    PLANNED = "PLANNED"
    IN_PROGRESS = "IN_PROGRESS"
    COMPLETED = "COMPLETED"


class Severity(str, Enum):
    UNSPECIFIED = "UNSPECIFIED"
    SEV_1 = "SEV-1"
    SEV_2 = "SEV-2"
    SEV_3 = "SEV-3"
    SEV_4 = "SEV-4"


class InvestigationAction(BaseModel):
    model_config = ConfigDict(extra="forbid")

    action: str = Field(min_length=1, max_length=500)
    timestamp: datetime = Field(default_factory=utc_now)
    result: ActionResult
    status: ActionStatus = ActionStatus.COMPLETED
    evidence: Optional[str] = Field(default=None, max_length=8000)
    notes: Optional[str] = Field(default=None, max_length=4000)
    reason_or_why: Optional[str] = Field(default=None, max_length=2000)

    @field_validator("result", mode="before")
    @classmethod
    def normalize_legacy_result(cls, value):
        if isinstance(value, str) and value.upper() == "SUCCESSFUL":
            return "SUCCESS"
        return value


class IncidentCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str = Field(min_length=3, max_length=240)
    service: str = Field(min_length=1, max_length=160)
    environment: str = Field(default="Production", min_length=1, max_length=80)
    error_message: str = Field(min_length=1, max_length=1000)
    symptoms: str = Field(min_length=1, max_length=12000)
    recent_changes: Optional[str] = Field(default=None, max_length=4000)
    logs_snippet: Optional[str] = Field(default=None, max_length=12000)
    severity: Severity = Severity.UNSPECIFIED
    impact: Optional[str] = Field(default=None, max_length=4000)
    owner: Optional[str] = Field(default=None, max_length=160)
    dependencies: List[str] = Field(default_factory=list, max_length=40)
    context: Optional[str] = Field(default=None, max_length=4000)


class IncidentResolutionUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    actions: List[InvestigationAction] = Field(default_factory=list, max_length=300)
    # Accept the earlier API field while clients transition to the complete action record.
    attempts: List[InvestigationAction] = Field(default_factory=list, max_length=300)
    successful_action: str = Field(min_length=1, max_length=2000)
    root_cause: str = Field(min_length=1, max_length=4000)
    lessons_learned: List[str] = Field(default_factory=list, max_length=100)

    def all_actions(self) -> List[InvestigationAction]:
        return self.actions + self.attempts


class HistoricalMatch(BaseModel):
    incident_id: str
    title: str
    service: str
    similarity_score: Optional[float] = None
    similarities: List[str] = Field(default_factory=list)
    differences: List[str] = Field(default_factory=list)
    failed_attempts: List[str] = Field(default_factory=list)
    successful_fix: Optional[str] = None
    root_cause: Optional[str] = None
    lesson_learned: Optional[str] = None


class IncidentAnalysisResult(BaseModel):
    summary: str
    recommendation_type: RecommendationType
    recommendation_summary: str
    detailed_recommendation: str
    historical_matches: List[HistoricalMatch] = Field(default_factory=list)
    key_similarities: List[str] = Field(default_factory=list)
    key_differences: List[str] = Field(default_factory=list)
    actions_to_avoid: List[str] = Field(default_factory=list)
    suggested_investigation_steps: List[str] = Field(default_factory=list)
    # This is an evidence sufficiency label, never a numerical confidence claim.
    confidence_level: str = "insufficient_evidence"
    reasoning_evidence: List[str] = Field(default_factory=list)
    current_observations: List[str] = Field(default_factory=list)
    historical_evidence: List[Dict[str, Any]] = Field(default_factory=list)
    inference: str = ""
    recommendation: str = ""


class TimelineEvent(BaseModel):
    id: str
    incident_id: str
    timestamp: datetime
    event_type: str
    message: str
    details: Dict[str, Any] = Field(default_factory=dict)


class IncidentRecord(BaseModel):
    id: str
    organization_id: str = "local"
    title: str
    service: str
    environment: str
    error_message: str
    symptoms: str
    recent_changes: Optional[str] = None
    logs_snippet: Optional[str] = None
    severity: Severity = Severity.UNSPECIFIED
    impact: Optional[str] = None
    owner: Optional[str] = None
    dependencies: List[str] = Field(default_factory=list)
    context: Optional[str] = None
    status: str = "INVESTIGATING"
    created_at: datetime = Field(default_factory=utc_now)
    resolved_at: Optional[datetime] = None
    actions: List[InvestigationAction] = Field(default_factory=list)
    attempts: List[InvestigationAction] = Field(default_factory=list)
    successful_action: Optional[str] = None
    root_cause: Optional[str] = None
    lessons_learned: List[str] = Field(default_factory=list)
    analysis: Optional[IncidentAnalysisResult] = None
    learning_saved: bool = False
    learning_error: Optional[str] = None
