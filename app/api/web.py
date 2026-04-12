from fastapi import APIRouter, Form, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from app.core.settings import ROOT_DIR
from app.core.store import JsonStore
from app.services.catalog import build_dashboard_payload, build_models_payload, build_tasks_payload, get_model_detail, load_archive_models
from app.services.auth import AuthManager, SESSION_COOKIE_NAME


router = APIRouter()
templates = Jinja2Templates(directory=str(ROOT_DIR / "app" / "templates"))
store = JsonStore()
auth_manager = AuthManager(store=store)


def _safe_next_path(value: str) -> str:
    candidate = str(value or "").strip()
    if not candidate.startswith("/"):
        return "/"
    return candidate


def _sample_detail() -> dict:
    return {
        "title": "Makerhub 详情页预览",
        "detail_path": "/detail-preview",
        "source_label": "MakerWorld 国内",
        "origin_url": "https://makerworld.com.cn/",
        "collect_date": "2026-04-11",
        "publish_date": "2026-03-09",
        "author": {
            "name": "makerhub",
            "url": "",
            "avatar_url": "https://placehold.co/128x128/e5e7eb/111827?text=MH",
        },
        "cover_url": "https://placehold.co/1080x1080/f8fafc/111827?text=Makerhub",
        "gallery": [
            {"url": "https://placehold.co/1080x1080/f8fafc/111827?text=Makerhub", "kind": "cover"},
            {"url": "https://placehold.co/1080x1080/e2e8f0/111827?text=Gallery+1", "kind": "design"},
            {"url": "https://placehold.co/1080x1080/e2e8f0/111827?text=Gallery+2", "kind": "design"},
        ],
        "tags": ["详情页", "预览", "makerhub"],
        "stats": {"likes": 0, "favorites": 0, "downloads": 0, "prints": 0, "views": 0, "comments": 0},
        "summary_html": """
        <p>当前没有真实归档模型，这里展示的是详情页占位版本。</p>
        <p>首页、模型库、设置、任务四个页面已经按真实归档数据结构重构完成。</p>
        """,
        "summary_text": "当前没有真实归档模型。",
        "comments": [],
        "instances": [
            {
                "instance_key": "preview-1",
                "title": "单色部分.3mf",
                "machine": "P1S",
                "time": "29.6 h",
                "plates": 20,
                "rating": "5.0",
                "publish_date": "2026-03-09",
                "download_count": 29,
                "print_count": 20,
                "summary": "预览实例，用于展示实例切换与 3MF 下载区域。",
                "thumbnail_url": "https://placehold.co/320x320/f1f5f9/111827?text=I1",
                "thumbnail_fallback_url": "",
                "primary_image_url": "https://placehold.co/1080x1080/f8fafc/111827?text=Instance+1",
                "primary_image_fallback_url": "",
                "media": [
                    {"label": "图1", "kind": "picture", "url": "https://placehold.co/1080x1080/f8fafc/111827?text=Instance+1", "fallback_url": ""},
                    {"label": "P1", "kind": "plate", "url": "https://placehold.co/1080x1080/e2e8f0/111827?text=P1", "fallback_url": ""},
                    {"label": "P2", "kind": "plate", "url": "https://placehold.co/1080x1080/dbeafe/111827?text=P2", "fallback_url": ""},
                ],
                "file_url": "/archive/preview/sample_1.3mf",
                "file_name": "sample_1.3mf",
            },
            {
                "instance_key": "preview-2",
                "title": "多色部分.3mf",
                "machine": "X1C",
                "time": "16 h",
                "plates": 8,
                "rating": "5.0",
                "publish_date": "2026-03-10",
                "download_count": 17,
                "print_count": 8,
                "summary": "第二个预览实例，用于展示打印实例切换。",
                "thumbnail_url": "https://placehold.co/320x320/e2e8f0/111827?text=I2",
                "thumbnail_fallback_url": "",
                "primary_image_url": "https://placehold.co/1080x1080/e2e8f0/111827?text=Instance+2",
                "primary_image_fallback_url": "",
                "media": [
                    {"label": "图1", "kind": "picture", "url": "https://placehold.co/1080x1080/e2e8f0/111827?text=Instance+2", "fallback_url": ""},
                    {"label": "P1", "kind": "plate", "url": "https://placehold.co/1080x1080/d1fae5/111827?text=P1", "fallback_url": ""},
                ],
                "file_url": "/archive/preview/sample_2.3mf",
                "file_name": "sample_2.3mf",
            },
        ],
        "attachments": [
            {
                "name": "组装手册.pdf",
                "category": "guide",
                "category_label": "组装指南",
                "url": "https://example.com/guide.pdf",
                "fallback_url": "",
                "ext": "pdf",
            }
        ],
    }


@router.get("/", response_class=HTMLResponse)
async def dashboard(request: Request):
    config = store.load()
    payload = build_dashboard_payload(config)
    return templates.TemplateResponse(
        "index.html",
        {
            "request": request,
            "active_page": "home",
            "show_sidebar": True,
            **payload,
        },
    )


@router.get("/login", response_class=HTMLResponse)
async def login_page(request: Request, next: str = Query("/")):
    identity = getattr(request.state, "auth_identity", None) or {}
    next_path = _safe_next_path(next)
    if identity.get("kind") == "session":
        return RedirectResponse(url=next_path, status_code=303)

    return templates.TemplateResponse(
        "login.html",
        {
            "request": request,
            "show_sidebar": False,
            "next_path": next_path,
            "error_message": "",
        },
    )


@router.post("/login", response_class=HTMLResponse)
async def login_submit(
    request: Request,
    username: str = Form(""),
    password: str = Form(""),
    next: str = Form("/"),
):
    next_path = _safe_next_path(next)
    if not auth_manager.authenticate_credentials(username, password):
        return templates.TemplateResponse(
            "login.html",
            {
                "request": request,
                "show_sidebar": False,
                "next_path": next_path,
                "error_message": "用户名或密码错误。",
            },
            status_code=401,
        )

    session = auth_manager.create_session(username)
    response = RedirectResponse(url=next_path, status_code=303)
    response.set_cookie(
        SESSION_COOKIE_NAME,
        session["id"],
        max_age=14 * 24 * 60 * 60,
        httponly=True,
        samesite="lax",
        path="/",
    )
    return response


@router.get("/logout")
async def logout(request: Request):
    identity = getattr(request.state, "auth_identity", None) or {}
    if identity.get("kind") == "session":
        auth_manager.delete_session(str(identity.get("session_id") or ""))
    response = RedirectResponse(url="/login", status_code=303)
    response.delete_cookie(SESSION_COOKIE_NAME, path="/")
    return response


@router.get("/models", response_class=HTMLResponse)
async def models_page(
    request: Request,
    q: str = Query(""),
    source: str = Query("all"),
    tag: str = Query(""),
    sort: str = Query("collectDate"),
):
    payload = build_models_payload(q=q, source=source, tag=tag, sort_key=sort)
    return templates.TemplateResponse(
        "models.html",
        {
            "request": request,
            "active_page": "models",
            "show_sidebar": True,
            **payload,
        },
    )


@router.get("/models/{model_dir:path}", response_class=HTMLResponse)
async def model_detail(request: Request, model_dir: str):
    detail = get_model_detail(model_dir)
    if not detail:
        raise HTTPException(status_code=404, detail="模型不存在")

    return templates.TemplateResponse(
        "detail.html",
        {
            "request": request,
            "active_page": "models",
            "show_sidebar": True,
            "detail": detail,
        },
    )


@router.get("/settings", response_class=HTMLResponse)
async def settings_page(request: Request):
    config = store.load()
    cookie_map = {item.platform: item.cookie for item in config.cookies}
    return templates.TemplateResponse(
        "settings.html",
        {
            "request": request,
            "active_page": "settings",
            "show_sidebar": True,
            "config": config,
            "cookie_map": cookie_map,
            "token_items": auth_manager.list_api_tokens(),
        },
    )


@router.get("/tasks", response_class=HTMLResponse)
async def tasks_page(request: Request):
    config = store.load()
    payload = build_tasks_payload(missing_fallback=[item.model_dump() for item in config.missing_3mf])
    return templates.TemplateResponse(
        "tasks.html",
        {
            "request": request,
            "active_page": "tasks",
            "show_sidebar": True,
            **payload,
        },
    )


@router.get("/detail-preview", response_class=HTMLResponse)
async def detail_preview(request: Request):
    models = load_archive_models()
    detail = models[0] if models else _sample_detail()
    return templates.TemplateResponse(
        "detail.html",
        {
            "request": request,
            "active_page": "models",
            "show_sidebar": True,
            "detail": detail,
        },
    )
