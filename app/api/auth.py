from fastapi import APIRouter, HTTPException, Request, Response
from fastapi.responses import JSONResponse

from app.core.store import JsonStore
from app.schemas.models import ApiTokenCreateRequest, LoginRequest, PasswordChangeRequest
from app.services.auth import AuthManager, SESSION_COOKIE_NAME, SESSION_TTL_DAYS
from app.services.business_logs import append_business_log


router = APIRouter(prefix="/api/auth")
store = JsonStore()
auth_manager = AuthManager(store=store)


def _require_session_auth(request: Request) -> dict:
    identity = getattr(request.state, "auth_identity", None) or {}
    if identity.get("kind") != "session":
        raise HTTPException(status_code=403, detail="此操作需要登录会话。")
    return identity


@router.post("/login")
async def login(payload: LoginRequest, request: Request):
    failure_key = auth_manager.login_failure_key(request, payload.username)
    reservation = auth_manager.reserve_login_attempt(failure_key)
    if not reservation.allowed:
        append_business_log(
            "auth",
            "login_rate_limited",
            "登录失败次数过多，暂时限制重试。",
            level="warning",
            username=payload.username,
            retry_after_seconds=reservation.retry_after,
        )
        raise HTTPException(status_code=429, detail=f"登录失败次数过多，请 {reservation.retry_after} 秒后再试。")

    if not auth_manager.authenticate_credentials(payload.username, payload.password):
        append_business_log(
            "auth",
            "login_failed",
            "登录失败：用户名或密码错误。",
            level="warning",
            username=payload.username,
            rate_limited=reservation.retry_after > 0,
        )
        raise HTTPException(status_code=401, detail="用户名或密码错误。")

    auth_manager.clear_login_failures(failure_key)
    session = auth_manager.create_session(payload.username)
    default_password = auth_manager.default_password_active()
    append_business_log(
        "auth",
        "login_success",
        "用户已登录。",
        username=payload.username,
        default_password=default_password,
    )
    if default_password:
        append_business_log(
            "auth",
            "default_password_active",
            "当前仍在使用默认 admin 密码，请尽快修改。",
            level="warning",
            username=payload.username,
        )
    response = JSONResponse({"success": True, "username": payload.username, "default_password": default_password})
    response.set_cookie(
        SESSION_COOKIE_NAME,
        session["id"],
        max_age=SESSION_TTL_DAYS * 24 * 60 * 60,
        httponly=True,
        samesite="lax",
        path="/",
        secure=_secure_cookie_for_request(request),
    )
    return response


@router.post("/logout")
async def logout(request: Request):
    identity = getattr(request.state, "auth_identity", None) or {}
    if identity.get("kind") == "session":
        auth_manager.delete_session(str(identity.get("session_id") or ""))
    append_business_log("auth", "logout", "用户已退出登录。", kind=identity.get("kind") or "")
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
        "default_password": auth_manager.default_password_active() if identity else False,
    }


def _secure_cookie_for_request(request: Request) -> bool:
    return str(request.url.scheme or "").lower() == "https"


def _token_list_payload(items) -> list[dict]:
    return [item.model_dump(exclude={"token_value"}) for item in items]


@router.get("/tokens")
async def list_tokens(request: Request):
    _require_session_auth(request)
    return {"items": _token_list_payload(auth_manager.list_api_tokens())}


@router.post("/tokens")
async def create_token(payload: ApiTokenCreateRequest, request: Request):
    _require_session_auth(request)
    raw_token, token_view = auth_manager.create_api_token(
        payload.name,
        permissions=payload.permissions,
        expires_days=payload.expires_days,
    )
    append_business_log("auth", "api_token_created", "API Token 已创建。", token_id=token_view.id, name=token_view.name)
    return {
        "success": True,
        "token": raw_token,
        "item": token_view.model_dump(),
        "items": _token_list_payload(auth_manager.list_api_tokens()),
    }


@router.delete("/tokens/{token_id}")
async def revoke_token(token_id: str, request: Request):
    _require_session_auth(request)
    items = auth_manager.revoke_api_token(token_id)
    append_business_log("auth", "api_token_revoked", "API Token 已撤销。", token_id=token_id)
    return {
        "success": True,
        "items": _token_list_payload(items),
    }


@router.post("/password")
async def change_password(payload: PasswordChangeRequest, request: Request):
    _require_session_auth(request)
    try:
        auth_manager.change_password(payload.current_password, payload.new_password)
    except ValueError as exc:
        append_business_log("auth", "password_change_failed", str(exc), level="warning")
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    append_business_log("auth", "password_changed", "登录密码已修改。")
    response = JSONResponse({"success": True, "message": "密码已更新，请重新登录。"})
    response.delete_cookie(SESSION_COOKIE_NAME, path="/")
    return response
