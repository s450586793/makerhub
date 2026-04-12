from urllib.parse import quote

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from app.api.auth import router as auth_router
from app.api.config import router as config_router
from app.api.web import router as web_router
from app.core.settings import ARCHIVE_DIR, ROOT_DIR, ensure_app_dirs
from app.services.auth import AuthManager


ensure_app_dirs()

app = FastAPI(title="makerhub", version="0.1.0")
auth_manager = AuthManager()


@app.middleware("http")
async def auth_guard(request: Request, call_next):
    path = request.url.path
    if path.startswith("/static"):
        return await call_next(request)

    allow_api_token = path.startswith("/api") or path.startswith("/archive")
    identity = auth_manager.resolve_request_auth(request, allow_api_token=allow_api_token)
    request.state.auth_identity = identity or {}

    if path == "/login":
        if identity and identity.get("kind") == "session":
            return RedirectResponse(url="/", status_code=303)
        return await call_next(request)

    if path == "/api/auth/login":
        return await call_next(request)

    if path == "/logout":
        return await call_next(request)

    if identity is None:
        if path.startswith("/api") or path.startswith("/archive"):
            return JSONResponse({"detail": "Unauthorized"}, status_code=401)
        next_path = path
        if request.url.query:
            next_path = f"{path}?{request.url.query}"
        return RedirectResponse(url=f"/login?next={quote(next_path, safe='/=?&')}", status_code=303)

    if path == "/login":
        return RedirectResponse(url="/", status_code=303)

    return await call_next(request)


app.mount("/static", StaticFiles(directory=str(ROOT_DIR / "app" / "static")), name="static")
app.mount("/archive", StaticFiles(directory=str(ARCHIVE_DIR)), name="archive")
app.include_router(web_router)
app.include_router(auth_router)
app.include_router(config_router)
