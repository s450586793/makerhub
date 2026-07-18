import time
from urllib.parse import quote, urlparse

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles

from app.api.auth import router as auth_router
from app.api.dependencies import store as global_store
from app.api.config import crawler as config_crawler
from app.api.config import local_organizer
from app.api.config import remote_refresh_manager
from app.api.config import source_library_manager
from app.api.config import subscription_manager
from app.api.config import router as config_router
from app.api.logs_routes import router as logs_router
from app.api.models_routes import router as models_router
from app.api.performance_routes import router as performance_router
from app.api.remote_refresh_routes import router as remote_refresh_router
from app.api.runtime_routes import router as runtime_router
from app.api.sharing_routes import router as sharing_router
from app.api.source_library_routes import router as source_library_router
from app.api.subscriptions_routes import router as subscriptions_router
from app.api.system import router as system_router
from app.api.tasks_routes import router as tasks_router
from app.api.web import router as web_router
from app.core.settings import (
    APP_VERSION,
    ARCHIVE_DIR,
    BACKGROUND_TASKS_ENABLED,
    FRONTEND_DIST_DIR,
    MAX_MANUAL_ATTACHMENT_BYTES,
    MAX_LOCAL_IMPORT_UPLOAD_BYTES,
    PROCESS_ROLE,
    ROOT_DIR,
    ensure_app_dirs,
)
from app.core.api_permissions import api_token_permission_for_request
from app.core.database import close_database_pool
from app.services.auth import AuthManager, ensure_secure_admin_credential
from app.services.business_logs import append_business_log, shutdown_business_log_writer
from app.services.performance import log_api_request_if_needed
from app.services.request_threads import run_web_io, shutdown_request_threads
from app.services.self_update import mark_update_started_after_restart
from app.services.state_events import start_state_event_listener


ensure_app_dirs()

app = FastAPI(title="makerhub", version=APP_VERSION)
auth_manager = AuthManager(store=global_store)
LOGO_PATH = ROOT_DIR / "app" / "static" / "img" / "makerhub-logo.png"

SPA_SHELL_PATHS = {
    "/",
    "/login",
    "/models",
    "/subscriptions",
    "/subscriptions/manage",
    "/settings",
    "/organizer",
    "/remote-refresh",
    "/tasks",
    "/logs",
    "/detail-preview",
}

UNSAFE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}


def _request_origin(request: Request) -> str:
    scheme = str(request.url.scheme or "").lower()
    host = str(request.headers.get("host") or "").strip().lower()
    if not scheme or not host:
        return ""
    return f"{scheme}://{host}"


def _origin_from_header(value: str) -> str:
    text = str(value or "").strip()
    if not text or text.lower() == "null":
        return ""
    parsed = urlparse(text)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return ""
    return f"{parsed.scheme.lower()}://{parsed.netloc.lower()}"


def _csrf_origin_is_valid(request: Request) -> bool:
    expected_origin = _request_origin(request)
    if not expected_origin:
        return False
    origin = _origin_from_header(request.headers.get("origin") or "")
    if origin:
        return origin == expected_origin
    referer = _origin_from_header(request.headers.get("referer") or "")
    return bool(referer and referer == expected_origin)


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
    if LOGO_PATH.exists():
        return FileResponse(LOGO_PATH, media_type="image/png")
    return Response(status_code=204)


@app.on_event("startup")
async def resume_archive_queue() -> None:
    credential_result = ensure_secure_admin_credential(global_store)
    if credential_result.source == "generated" or credential_result.bootstrap_path.exists():
        print(f"[makerhub] admin bootstrap password file: {credential_result.bootstrap_path}", flush=True)
    mark_update_started_after_restart()
    start_state_event_listener()
    queue = {"recovered_count": 0, "queued_count": 0}
    if BACKGROUND_TASKS_ENABLED:
        queue = config_crawler.manager.resume_pending_tasks()
        subscription_manager.start()
        local_organizer.start()
        source_library_manager.start()
        remote_refresh_manager.start()
    recovered_count = int(queue.get("recovered_count") or 0)
    queued_count = int(queue.get("queued_count") or 0)
    append_business_log(
        "system",
        "app_started",
        "makerhub 已启动。",
        app_version=APP_VERSION,
        process_role=PROCESS_ROLE,
        background_tasks_enabled=BACKGROUND_TASKS_ENABLED,
        queued_count=queued_count,
        recovered_active=recovered_count,
    )
    if queued_count:
        print(f"[makerhub] archive queue resumed queued={queued_count} recovered_active={recovered_count}", flush=True)


@app.on_event("shutdown")
async def shutdown_thread_pools() -> None:
    local_organizer.stop()
    shutdown_business_log_writer()
    shutdown_request_threads()
    close_database_pool()


@app.middleware("http")
async def auth_guard(request: Request, call_next):
    started_perf = time.perf_counter()
    path = request.url.path

    def finish(response):
        response = _apply_cache_headers(path, response)
        if path.startswith("/api/"):
            duration_ms = (time.perf_counter() - started_perf) * 1000
            log_api_request_if_needed(request, response, duration_ms=duration_ms)
        return response

    if (
        request.method == "POST"
        and (
            (path.startswith("/api/models/") and path.endswith("/attachments"))
            or path == "/api/mobile-import/raw"
            or path == "/api/mobile-import/raw-ipv4"
        )
    ):
        content_length = str(request.headers.get("content-length") or "").strip()
        if not content_length and path != "/api/mobile-import/raw-ipv4":
            return finish(JSONResponse({"detail": "上传请求缺少 Content-Length。"}, status_code=411))
        if content_length:
            try:
                body_size = int(content_length)
            except ValueError:
                return finish(JSONResponse({"detail": "上传请求大小无效。"}, status_code=400))
            if path in {"/api/mobile-import/raw", "/api/mobile-import/raw-ipv4"}:
                max_upload_bytes = MAX_LOCAL_IMPORT_UPLOAD_BYTES
            else:
                max_upload_bytes = MAX_MANUAL_ATTACHMENT_BYTES
            if body_size > max_upload_bytes:
                return finish(JSONResponse({"detail": "上传文件过大。"}, status_code=413))

    if path.startswith("/static") or path.startswith("/assets"):
        response = await call_next(request)
        return finish(response)

    api_token_permission = api_token_permission_for_request(request.method, path)
    identity = await run_web_io(
        auth_manager.resolve_request_auth,
        request,
        api_token_permission=api_token_permission,
    )
    request.state.auth_identity = identity or {}

    if path == "/login":
        if identity and identity.get("kind") == "session":
            return finish(RedirectResponse(url="/", status_code=303))
        response = await call_next(request)
        return finish(response)

    if path == "/api/auth/login":
        response = await call_next(request)
        return finish(response)

    if path == "/api/bootstrap" or path.startswith("/api/public/") or path == "/api/mobile-import" or path.startswith("/api/mobile-import/"):
        response = await call_next(request)
        return finish(response)

    if identity is None:
        if path.startswith("/api") or path.startswith("/archive"):
            return finish(JSONResponse({"detail": "Unauthorized"}, status_code=401))
        next_path = path
        if request.url.query:
            next_path = f"{path}?{request.url.query}"
        return finish(
            RedirectResponse(url=f"/login?next={quote(next_path, safe='/=?&')}", status_code=303),
        )

    if path == "/login":
        return finish(RedirectResponse(url="/", status_code=303))

    if (
        identity.get("kind") == "session"
        and request.method.upper() in UNSAFE_METHODS
        and path.startswith("/api/")
        and not _csrf_origin_is_valid(request)
    ):
        return finish(JSONResponse({"detail": "CSRF origin check failed"}, status_code=403))

    response = await call_next(request)
    return finish(response)


app.mount("/static", StaticFiles(directory=str(ROOT_DIR / "app" / "static")), name="static")
if FRONTEND_DIST_DIR.exists():
    app.mount("/assets", StaticFiles(directory=str(FRONTEND_DIST_DIR / "assets")), name="assets")
app.mount("/archive", StaticFiles(directory=str(ARCHIVE_DIR)), name="archive")
app.include_router(web_router)
app.include_router(auth_router)
app.include_router(system_router)
app.include_router(config_router)
app.include_router(logs_router)
app.include_router(models_router)
app.include_router(performance_router)
app.include_router(remote_refresh_router)
app.include_router(runtime_router)
app.include_router(sharing_router)
app.include_router(source_library_router)
app.include_router(subscriptions_router)
app.include_router(tasks_router)
