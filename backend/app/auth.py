import hashlib
import hmac
import ipaddress
import json
import os
from dataclasses import dataclass

from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

bearer = HTTPBearer(auto_error=False)


@dataclass(frozen=True)
class Tenant:
    organization_id: str
    local_demo: bool = False


def _token_map() -> dict[str, str]:
    raw = os.getenv("AFTERMATH_API_TOKENS", "")
    if not raw:
        return {}
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=503, detail="Authentication is misconfigured") from exc
    if not isinstance(value, dict) or any(not isinstance(token, str) or len(token) < 32 or not isinstance(org, str) or not org for token, org in value.items()):
        raise HTTPException(status_code=503, detail="Authentication is misconfigured")
    return value


def _is_loopback_request(request: Request) -> bool:
    client = request.client
    if client is None:
        return False
    try:
        return ipaddress.ip_address(client.host.split("%", 1)[0]).is_loopback
    except ValueError:
        return False


def get_current_tenant(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = Depends(bearer),
) -> Tenant:
    token_map = _token_map()
    environment = os.getenv("AFTERMATH_ENVIRONMENT", "development").strip().lower()
    demo_enabled = os.getenv("AFTERMATH_LOCAL_DEMO", "false").strip().lower() in {"1", "true", "yes", "on"}
    if (
        credentials is None
        and environment != "production"
        and demo_enabled
        and _is_loopback_request(request)
    ):
        organization_id = os.getenv("AFTERMATH_DEMO_ORGANIZATION_ID", "local-demo").strip()
        if not organization_id:
            raise HTTPException(status_code=503, detail="Local demo organization is misconfigured")
        return Tenant(organization_id=organization_id, local_demo=True)
    if not token_map:
        raise HTTPException(status_code=503, detail="API authentication is not configured")
    if credentials is None or credentials.scheme.lower() != "bearer":
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Bearer token required", headers={"WWW-Authenticate": "Bearer"})
    supplied = hashlib.sha256(credentials.credentials.encode()).digest()
    for configured, organization_id in token_map.items():
        if hmac.compare_digest(supplied, hashlib.sha256(configured.encode()).digest()):
            return Tenant(organization_id=organization_id)
    raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid bearer token", headers={"WWW-Authenticate": "Bearer"})
