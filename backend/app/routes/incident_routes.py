import logging
from typing import List

from fastapi import APIRouter, Depends, HTTPException, status

from app.auth import Tenant, get_current_tenant
from app.schemas.incident import IncidentAnalysisResult, IncidentCreate, IncidentRecord, IncidentResolutionUpdate, InvestigationAction, TimelineEvent
from app.services.hindsight_service import hindsight_service
from app.services.incident_service import incident_service
from app.services.llm_service import llm_service

logger = logging.getLogger("aftermath.api")
router = APIRouter(prefix="/api", tags=["incidents"], dependencies=[Depends(get_current_tenant)])


@router.get("/runtime")
def get_runtime(tenant: Tenant = Depends(get_current_tenant)):
    return {
        "organization_id": tenant.organization_id,
        "workspace_mode": "DEMO" if tenant.local_demo else "AUTHENTICATED",
        "memory": hindsight_service.runtime_status(),
        "reasoning": llm_service.runtime_status(),
    }


@router.get("/incidents", response_model=List[IncidentRecord])
def get_incidents(tenant: Tenant = Depends(get_current_tenant)):
    return incident_service.list_incidents(tenant.organization_id)


@router.post("/incidents", response_model=IncidentRecord, status_code=status.HTTP_201_CREATED)
def create_incident(payload: IncidentCreate, tenant: Tenant = Depends(get_current_tenant)):
    return incident_service.create_incident(payload, tenant.organization_id)


@router.get("/incidents/{incident_id}", response_model=IncidentRecord)
def get_incident(incident_id: str, tenant: Tenant = Depends(get_current_tenant)):
    incident = incident_service.get_incident(incident_id, tenant.organization_id)
    if not incident:
        raise HTTPException(status_code=404, detail="Incident not found")
    return incident


@router.post("/incidents/{incident_id}/analyze", response_model=IncidentAnalysisResult)
def analyze_incident(incident_id: str, tenant: Tenant = Depends(get_current_tenant)):
    try:
        return incident_service.analyze_incident(incident_id, tenant.organization_id)
    except ValueError:
        raise HTTPException(status_code=404, detail="Incident not found")
    except Exception:
        logger.exception("Analysis endpoint failed for incident %s", incident_id)
        raise HTTPException(status_code=503, detail="Analysis is temporarily unavailable")


@router.post("/incidents/{incident_id}/actions", response_model=IncidentRecord, status_code=status.HTTP_201_CREATED)
def record_action(incident_id: str, action: InvestigationAction, tenant: Tenant = Depends(get_current_tenant)):
    try:
        return incident_service.record_action(incident_id, action, tenant.organization_id)
    except ValueError as exc:
        code = 404 if str(exc) == "Incident not found" else 409
        raise HTTPException(status_code=code, detail=str(exc))


@router.get("/incidents/{incident_id}/timeline", response_model=List[TimelineEvent])
def get_timeline(incident_id: str, tenant: Tenant = Depends(get_current_tenant)):
    try:
        return incident_service.list_events(incident_id, tenant.organization_id)
    except ValueError:
        raise HTTPException(status_code=404, detail="Incident not found")


@router.post("/incidents/{incident_id}/resolve", response_model=IncidentRecord)
def resolve_incident(incident_id: str, resolution: IncidentResolutionUpdate, tenant: Tenant = Depends(get_current_tenant)):
    try:
        return incident_service.resolve_incident(incident_id, resolution, tenant.organization_id)
    except ValueError:
        raise HTTPException(status_code=404, detail="Incident not found")
    except Exception:
        logger.exception("Resolution endpoint failed for incident %s", incident_id)
        raise HTTPException(status_code=503, detail="Resolution was not saved. Retry after checking service health.")


@router.get("/memories")
def get_memories(tenant: Tenant = Depends(get_current_tenant)):
    return {
        "bank_id": hindsight_service.bank_id,
        "runtime": hindsight_service.runtime_status(),
        "total_memories": len(hindsight_service.database.list_memories(tenant.organization_id)),
        "memories": hindsight_service.database.list_memories(tenant.organization_id),
    }
