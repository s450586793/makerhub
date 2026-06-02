from __future__ import annotations

from fastapi import APIRouter, Query

from app.services.business_logs import read_log_entries
from app.services.request_threads import run_web_io


router = APIRouter(prefix="/api")


@router.get("/logs")
async def get_logs_data(
    file: str = Query("business.log", description="日志文件名"),
    limit: int = Query(300, ge=1, le=2000, description="最多返回行数"),
    q: str = Query("", description="日志内容搜索"),
):
    return await run_web_io(read_log_entries, file_name=file, limit=limit, query=q)
