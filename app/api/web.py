from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from app.core.settings import ROOT_DIR
from app.core.store import JsonStore
from app.services.catalog import build_dashboard_payload, build_models_payload, build_tasks_payload, get_model_detail, load_archive_models


router = APIRouter()
templates = Jinja2Templates(directory=str(ROOT_DIR / "app" / "templates"))
store = JsonStore()


def _sample_detail() -> dict:
    return {
        "title": "Makerhub 详情页预览",
        "detail_path": "/detail-preview",
        "source_label": "MakerWorld 国内",
        "origin_url": "https://makerworld.com.cn/",
        "collect_date": "2026-04-11",
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
        "instances": [],
        "attachments": [],
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
            **payload,
        },
    )


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
            "config": config,
            "cookie_map": cookie_map,
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
            "detail": detail,
        },
    )
