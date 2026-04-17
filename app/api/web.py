from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse

from app.core.settings import FRONTEND_INDEX_PATH
from app.core.store import JsonStore
from app.services.auth import AuthManager, SESSION_COOKIE_NAME


router = APIRouter()
store = JsonStore()
auth_manager = AuthManager(store=store)


def _serve_spa() -> FileResponse:
    if not FRONTEND_INDEX_PATH.exists():
        raise HTTPException(status_code=503, detail="前端资源尚未构建，请先执行前端打包。")
    return FileResponse(FRONTEND_INDEX_PATH)


@router.get("/", response_class=HTMLResponse)
async def dashboard(_: Request):
    return _serve_spa()


@router.get("/login", response_class=HTMLResponse)
async def login_page(_: Request):
    return _serve_spa()


@router.get("/models", response_class=HTMLResponse)
async def models_page(_: Request):
    return _serve_spa()


@router.get("/subscriptions", response_class=HTMLResponse)
async def subscriptions_page(_: Request):
    return _serve_spa()


@router.get("/models/{model_dir:path}", response_class=HTMLResponse)
async def model_detail(_: Request, model_dir: str):
    return _serve_spa()


@router.get("/settings", response_class=HTMLResponse)
async def settings_page(_: Request):
    return _serve_spa()


@router.get("/organizer", response_class=HTMLResponse)
async def organizer_page(_: Request):
    return _serve_spa()


@router.get("/remote-refresh", response_class=HTMLResponse)
async def remote_refresh_page(_: Request):
    return _serve_spa()


@router.get("/tasks", response_class=HTMLResponse)
async def tasks_page(_: Request):
    return _serve_spa()


@router.get("/logs", response_class=HTMLResponse)
async def logs_page(_: Request):
    return _serve_spa()


@router.get("/detail-preview", response_class=HTMLResponse)
async def detail_preview(_: Request):
    return _serve_spa()


@router.get("/logout")
async def logout(request: Request):
    identity = getattr(request.state, "auth_identity", None) or {}
    if identity.get("kind") == "session":
        auth_manager.delete_session(str(identity.get("session_id") or ""))
    response = RedirectResponse(url="/login", status_code=303)
    response.delete_cookie(SESSION_COOKIE_NAME, path="/")
    return response
