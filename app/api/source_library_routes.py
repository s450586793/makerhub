from __future__ import annotations

import re

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import FileResponse

from app.api.dependencies import store, task_state_store
from app.services.request_threads import run_web_io
from app.services.source_library import (
    SOURCE_LIBRARY_SNAPSHOT_DIR,
    build_source_group_models_payload,
    build_source_library_payload,
    build_state_group_models_payload,
)


router = APIRouter(prefix="/api")


@router.get("/source-library")
async def get_source_library_data(
    q: str = Query("", description="搜索来源卡标题"),
):
    return await run_web_io(
        build_source_library_payload,
        q=q,
        store=store,
        task_store=task_state_store,
    )


@router.get("/source-library/snapshots/{filename}")
async def get_source_library_snapshot(filename: str):
    if not re.fullmatch(r"[A-Za-z0-9_.-]+\.webp", str(filename or "")):
        raise HTTPException(status_code=404, detail="快照不存在。")
    snapshot_root = SOURCE_LIBRARY_SNAPSHOT_DIR.resolve()
    target = (snapshot_root / filename).resolve()
    try:
        target.relative_to(snapshot_root)
    except ValueError:
        raise HTTPException(status_code=404, detail="快照不存在。") from None
    if not target.is_file():
        raise HTTPException(status_code=404, detail="快照不存在。")
    return FileResponse(target, media_type="image/webp")


@router.get("/source-library/sources/{source_type}/{source_key}")
async def get_source_group_models(
    source_type: str,
    source_key: str,
    q: str = Query("", description="搜索标题、作者、标签"),
    source: str = Query("all", description="全部 / 国内 / 国际 / 本地"),
    tag: str = Query("", description="按标签过滤"),
    sort: str = Query("collectDate", description="collectDate / downloads / likes / prints"),
    page: int = Query(1, ge=1, description="分页页码"),
    page_size: int = Query(8, ge=1, le=120, description="每页数量"),
    limit: int = Query(0, ge=0, le=2000, description="从第一页起一次返回的数量"),
):
    effective_page_size = limit if limit > 0 else page_size
    payload = await run_web_io(
        build_source_group_models_payload,
        source_type=source_type,
        source_key=source_key,
        q=q,
        source=source,
        tag=tag,
        sort_key=sort,
        page=1 if limit > 0 else page,
        page_size=effective_page_size,
        store=store,
        task_store=task_state_store,
    )
    if payload is None:
        raise HTTPException(status_code=404, detail="来源不存在。")
    if limit > 0:
        payload["page"] = max(int(page or 1), 1)
        payload["page_size"] = page_size
    return payload


@router.get("/source-library/states/{state_key}")
async def get_state_group_models(
    state_key: str,
    q: str = Query("", description="搜索标题、作者、标签"),
    source: str = Query("all", description="全部 / 国内 / 国际 / 本地"),
    tag: str = Query("", description="按标签过滤"),
    sort: str = Query("collectDate", description="collectDate / downloads / likes / prints"),
    page: int = Query(1, ge=1, description="分页页码"),
    page_size: int = Query(8, ge=1, le=120, description="每页数量"),
    limit: int = Query(0, ge=0, le=2000, description="从第一页起一次返回的数量"),
):
    effective_page_size = limit if limit > 0 else page_size
    payload = await run_web_io(
        build_state_group_models_payload,
        state_key=state_key,
        q=q,
        source=source,
        tag=tag,
        sort_key=sort,
        page=1 if limit > 0 else page,
        page_size=effective_page_size,
        store=store,
        task_store=task_state_store,
    )
    if payload is None:
        raise HTTPException(status_code=404, detail="状态卡不存在。")
    if limit > 0:
        payload["page"] = max(int(page or 1), 1)
        payload["page_size"] = page_size
    return payload
