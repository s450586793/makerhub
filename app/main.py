from urllib.parse import quote

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles

from app.api.auth import router as auth_router
from app.api.config import crawler as config_crawler
from app.api.config import local_organizer
from app.api.config import remote_refresh_manager
from app.api.config import subscription_manager
from app.api.config import router as config_router
from app.api.web import router as web_router
from app.core.settings import APP_VERSION, ARCHIVE_DIR, FRONTEND_DIST_DIR, ROOT_DIR, ensure_app_dirs
from app.services.auth import AuthManager
from app.services.business_logs import append_business_log
from app.services.self_update import mark_update_started_after_restart


ensure_app_dirs()

app = FastAPI(title="makerhub", version=APP_VERSION)
auth_manager = AuthManager()

SPA_SHELL_PATHS = {
    "/",
    "/login",
    "/models",
    "/subscriptions",
    "/settings",
    "/organizer",
    "/remote-refresh",
    "/tasks",
    "/logs",
    "/detail-preview",
}


def _apply_cache_headers(path: str, response):
    if path.startswith("/assets/"):
        response.headers.setdefault("Cache-Control", "public, max-age=31536000, immutable")
        return response

    if path.startswith("/archive/"):
        response.headers.setdefault("Cache-Control", "public, max-age=3600")
        return response

    if path.startswith("/static/css/") or path.startswith("/static/js/"):
        response.headers["Cache-Control"] = "public, no-cache, must-revalidate"
        return response

    if (
        path.startswith("/api/")
        or path.startswith("/static/")
        or path in SPA_SHELL_PATHS
        or path.startswith("/models/")
    ):
        response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate"
        response.headers["Pragma"] = "no-cache"
        response.headers["Expires"] = "0"

    return response


@app.get("/favicon.ico", include_in_schema=False)
async def favicon() -> Response:
    return Response(status_code=204)


@app.on_event("startup")
async def resume_archive_queue() -> None:
    mark_update_started_after_restart()
    queue = config_crawler.manager.resume_pending_tasks()
    subscription_manager.start()
    local_organizer.start()
    remote_refresh_manager.start()
    recovered_count = int(queue.get("recovered_count") or 0)
    queued_count = int(queue.get("queued_count") or 0)
    append_business_log(
        "system",
        "app_started",
        "makerhub 已启动。",
        app_version=APP_VERSION,
        queued_count=queued_count,
        recovered_active=recovered_count,
    )
    if queued_count:
        print(f"[makerhub] archive queue resumed queued={queued_count} recovered_active={recovered_count}", flush=True)


@app.middleware("http")
async def auth_guard(request: Request, call_next):
    path = request.url.path
    if path.startswith("/static") or path.startswith("/assets"):
        response = await call_next(request)
        return _apply_cache_headers(path, response)

    allow_api_token = path.startswith("/api") or path.startswith("/archive")
    identity = auth_manager.resolve_request_auth(request, allow_api_token=allow_api_token)
    request.state.auth_identity = identity or {}

    if path == "/login":
        if identity and identity.get("kind") == "session":
            return _apply_cache_headers(path, RedirectResponse(url="/", status_code=303))
        response = await call_next(request)
        return _apply_cache_headers(path, response)

    if path == "/api/auth/login":
        response = await call_next(request)
        return _apply_cache_headers(path, response)

    if path == "/api/bootstrap":
        response = await call_next(request)
        return _apply_cache_headers(path, response)

    if identity is None:
        if path.startswith("/api") or path.startswith("/archive"):
            return _apply_cache_headers(path, JSONResponse({"detail": "Unauthorized"}, status_code=401))
        next_path = path
        if request.url.query:
            next_path = f"{path}?{request.url.query}"
        return _apply_cache_headers(
            path,
            RedirectResponse(url=f"/login?next={quote(next_path, safe='/=?&')}", status_code=303),
        )

    if path == "/login":
        return _apply_cache_headers(path, RedirectResponse(url="/", status_code=303))

    response = await call_next(request)
    return _apply_cache_headers(path, response)


app.mount("/static", StaticFiles(directory=str(ROOT_DIR / "app" / "static")), name="static")
if FRONTEND_DIST_DIR.exists():
    app.mount("/assets", StaticFiles(directory=str(FRONTEND_DIST_DIR / "assets")), name="assets")
app.mount("/archive", StaticFiles(directory=str(ARCHIVE_DIR)), name="archive")
app.include_router(web_router)
app.include_router(auth_router)
app.include_router(config_router)
