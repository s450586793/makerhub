from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

from app.api.config import _require_session_auth
from app.api.dependencies import store
from app.services.account_manual_verification import ManualVerificationError, manual_verification_manager
from app.services.request_threads import run_task_api


router = APIRouter(prefix="/api/config/online-accounts")


class VerificationProof(BaseModel):
    model_config = ConfigDict(extra="forbid")

    captcha_id: str = Field(pattern=r"^[0-9a-fA-F]{32}$")
    lot_number: str = Field(min_length=1, max_length=128)
    pass_token: str = Field(min_length=1, max_length=512)
    gen_time: str = Field(pattern=r"^[0-9]{10,13}$")
    captcha_output: str = Field(min_length=1, max_length=8192)


class VerificationComplete(BaseModel):
    model_config = ConfigDict(extra="forbid")

    challenge_id: str = Field(pattern=r"^[0-9a-f]{32}$")
    proof: VerificationProof


def _owner(request: Request) -> str:
    _require_session_auth(request)
    identity = getattr(request.state, "auth_identity", None) or {}
    if identity.get("username") != store.load().user.username or not identity.get("session_id"):
        raise HTTPException(status_code=403, detail="此操作需要管理员登录会话。")
    return str(identity["session_id"])


async def _execute(request: Request, platform: str, session_id: str, action: str, **payload):
    owner = _owner(request)
    try:
        return await run_task_api(manual_verification_manager.execute, platform, session_id, owner, action, **payload)
    except ManualVerificationError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from None


@router.post("/{platform}/verification")
async def create_verification(platform: str, request: Request):
    owner = _owner(request)
    try:
        return await run_task_api(manual_verification_manager.create, platform, owner)
    except ManualVerificationError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from None


@router.get("/{platform}/verification/{session_id}")
async def verification_status(platform: str, session_id: str, request: Request):
    return await _execute(request, platform, session_id, "status")


@router.post("/{platform}/verification/{session_id}/complete")
async def complete_verification(platform: str, session_id: str, payload: VerificationComplete, request: Request):
    return await _execute(request, platform, session_id, "complete",
                          challenge_id=payload.challenge_id, proof=payload.proof.model_dump())


@router.delete("/{platform}/verification/{session_id}")
async def cancel_verification(platform: str, session_id: str, request: Request):
    return await _execute(request, platform, session_id, "close")
