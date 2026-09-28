import logging
import os
import uuid

import uvicorn
from dotenv import load_dotenv
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

load_dotenv()

from app.auth import _token_map
from app.routes.incident_routes import router as incident_router
from app.services.hindsight_service import hindsight_service
from app.services.incident_service import incident_service
from app.services.storage_service import store

logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))
logger = logging.getLogger("aftermath")
environment = os.getenv("AFTERMATH_ENVIRONMENT", "development").lower()
app = FastAPI(title="AFTERMATH API", description="Evidence-grounded incident learning API", version="2.0.0")

origins = [origin.strip() for origin in os.getenv("AFTERMATH_CORS_ORIGINS", "http://localhost:5173,http://127.0.0.1:5173").split(",") if origin.strip()]
if "*" in origins:
    raise RuntimeError("AFTERMATH_CORS_ORIGINS must not contain a wildcard origin")
app.add_middleware(CORSMiddleware, allow_origins=origins, allow_credentials=False, allow_methods=["GET", "POST", "OPTIONS"], allow_headers=["Authorization", "Content-Type"], max_age=600)
app.include_router(incident_router)


@app.on_event("startup")
def startup_event():
    logger.info("AFTERMATH startup: environment=%s memory_mode=%s", environment, hindsight_service.mode)
    if not store.health():
        raise RuntimeError("Incident database is unavailable")
    if environment == "production":
        if not _token_map():
            raise RuntimeError("AFTERMATH_API_TOKENS must be configured in production")
        if hindsight_service.mode != "hindsight" or not hindsight_service.runtime_status()["connected"]:
            raise RuntimeError("Production requires a verified Hindsight connection; refusing local fallback")


@app.get("/")
def read_root():
    return {"name": "AFTERMATH API", "status": "online", "version": app.version}


@app.get("/health/live")
def liveness():
    return {"status": "alive"}


@app.get("/health/ready")
def readiness():
    db_ready = store.health()
    memory = hindsight_service.runtime_status()
    memory_ready = memory["available"] if environment != "production" else memory["connected"]
    ready = db_ready and memory_ready
    body = {"status": "ready" if ready else "degraded", "database": "available" if db_ready else "unavailable", "memory_mode": memory["mode"]}
    return JSONResponse(body, status_code=200 if ready else 503)


@app.exception_handler(Exception)
async def unhandled_exception(request: Request, exc: Exception):
    request_id = str(uuid.uuid4())
    logger.exception("Unhandled request error id=%s path=%s", request_id, request.url.path, exc_info=exc)
    return JSONResponse(status_code=500, content={"detail": "An unexpected server error occurred.", "request_id": request_id})


if __name__ == "__main__":
    uvicorn.run("app.main:app", host="0.0.0.0", port=int(os.getenv("PORT", "8000")), reload=environment != "production")
