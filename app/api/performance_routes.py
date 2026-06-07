from __future__ import annotations

from fastapi import APIRouter, Request

from app.api.config import _require_session_auth
from app.services.performance import log_frontend_page_event


router = APIRouter(prefix="/api")


@router.post("/performance/events")
async def record_performance_event(payload: dict, request: Request):
    _require_session_auth(request)
    return log_frontend_page_event(payload)
