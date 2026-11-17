from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

from app.api.config import _require_session_auth
from app.api.dependencies import store
from app.services.account_manual_verification import ManualVerificationError, manual_verification_manager
from app.services.request_threads import run_task_api


router = APIRouter(prefix="/api/config/online-accounts")


class VerificationInputEvent(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    type: Literal["pointerdown", "pointermove", "pointerup", "wheel", "key", "text"]
    x: float = Field(default=0, ge=0, le=4096)
    y: float = Field(default=0, ge=0, le=4096)
    delta_y: float = Field(default=0, ge=-2000, le=2000)
    delay_ms: float = Field(default=0, ge=0, le=50)
    key: str = Field(default="", max_length=50)
    text: str = Field(default="", max_length=2000)


class VerificationInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    events: list[VerificationInputEvent] = Field(min_length=1, max_length=80)


def _owner(request: Request) -> str:
    _require_session_auth(request)
    identity = getattr(request.state, "auth_identity", None) or {}
    if identity.get("username") != store.load().user.username or not identity.get("session_id"):
        raise HTTPException(status_code=403, detail="此操作需要管理员登录会话。")
    return str(identity["session_id"])


async def _execute(request: Request, platform: str, session_id: str, action: str, *, events=None):
    owner = _owner(request)
    try:
        return await run_task_api(manual_verification_manager.execute, platform, session_id, owner, action, events=events)
    except ManualVerificationError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from None


@router.post("/{platform}/verification")
async def create_verification(platform: str, request: Request):
    owner = _owner(request)
    try:
        return await run_task_api(manual_verification_manager.create, platform, owner)
    except ManualVerificationError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from None


@router.post("/{platform}/verification/{session_id}/frame")
async def verification_frame(platform: str, session_id: str, request: Request):
    return await _execute(request, platform, session_id, "frame")


@router.post("/{platform}/verification/{session_id}/input")
async def verification_input(platform: str, session_id: str, payload: VerificationInput, request: Request):
    return await _execute(request, platform, session_id, "input", events=[event.model_dump() for event in payload.events])


@router.post("/{platform}/verification/{session_id}/finish")
async def finish_verification(platform: str, session_id: str, request: Request):
    return await _execute(request, platform, session_id, "finish")


@router.delete("/{platform}/verification/{session_id}")
async def cancel_verification(platform: str, session_id: str, request: Request):
    return await _execute(request, platform, session_id, "close")
