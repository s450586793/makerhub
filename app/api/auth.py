from fastapi import APIRouter, HTTPException, Request, Response
from fastapi.responses import JSONResponse

from app.core.store import JsonStore
from app.schemas.models import ApiTokenCreateRequest, LoginRequest, PasswordChangeRequest
from app.services.auth import AuthManager, SESSION_COOKIE_NAME, SESSION_TTL_DAYS


router = APIRouter(prefix="/api/auth")
store = JsonStore()
auth_manager = AuthManager(store=store)


def _require_session_auth(request: Request) -> dict:
    identity = getattr(request.state, "auth_identity", None) or {}
    if identity.get("kind") != "session":
        raise HTTPException(status_code=403, detail="此操作需要登录会话。")
    return identity


@router.post("/login")
async def login(payload: LoginRequest):
    if not auth_manager.authenticate_credentials(payload.username, payload.password):
        raise HTTPException(status_code=401, detail="用户名或密码错误。")

    session = auth_manager.create_session(payload.username)
    response = JSONResponse({"success": True, "username": payload.username})
    response.set_cookie(
        SESSION_COOKIE_NAME,
        session["id"],
        max_age=SESSION_TTL_DAYS * 24 * 60 * 60,
        httponly=True,
        samesite="lax",
        path="/",
    )
    return response


@router.post("/logout")
async def logout(request: Request):
    identity = getattr(request.state, "auth_identity", None) or {}
    if identity.get("kind") == "session":
        auth_manager.delete_session(str(identity.get("session_id") or ""))
    response = JSONResponse({"success": True})
    response.delete_cookie(SESSION_COOKIE_NAME, path="/")
    return response


@router.get("/me")
async def me(request: Request):
    identity = getattr(request.state, "auth_identity", None) or {}
    config = store.load()
    return {
        "authenticated": bool(identity),
        "kind": identity.get("kind") or "",
        "username": config.user.username if identity else "",
        "display_name": config.user.display_name if identity else "",
    }


@router.get("/tokens")
async def list_tokens(request: Request):
    _require_session_auth(request)
    return {"items": [item.model_dump() for item in auth_manager.list_api_tokens()]}


@router.post("/tokens")
async def create_token(payload: ApiTokenCreateRequest, request: Request):
    _require_session_auth(request)
    raw_token, token_view = auth_manager.create_api_token(payload.name)
    return {
        "success": True,
        "token": raw_token,
        "item": token_view.model_dump(),
        "items": [item.model_dump() for item in auth_manager.list_api_tokens()],
    }


@router.delete("/tokens/{token_id}")
async def revoke_token(token_id: str, request: Request):
    _require_session_auth(request)
    items = auth_manager.revoke_api_token(token_id)
    return {
        "success": True,
        "items": [item.model_dump() for item in items],
    }


@router.post("/password")
async def change_password(payload: PasswordChangeRequest, request: Request):
    _require_session_auth(request)
    try:
        auth_manager.change_password(payload.current_password, payload.new_password)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    response = JSONResponse({"success": True, "message": "密码已更新，请重新登录。"})
    response.delete_cookie(SESSION_COOKIE_NAME, path="/")
    return response
