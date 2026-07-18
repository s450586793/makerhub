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
    level: str = Query("", description="日志级别，多个值用逗号分隔"),
    category: str = Query("", description="日志分类，多个值用逗号分隔"),
    event: str = Query("", description="日志事件，多个值用逗号分隔"),
    since: str = Query("", description="起始时间 ISO 字符串"),
    cursor: str = Query("", description="分页游标"),
    include_facets: bool = Query(True, description="是否计算筛选聚合"),
    include_files: bool = Query(True, description="是否读取日志文件聚合"),
):
    return await run_web_io(
        read_log_entries,
        file_name=file,
        limit=limit,
        query=q,
        level=level,
        category=category,
        event=event,
        since=since,
        cursor=cursor,
        include_facets=include_facets,
        include_files=include_files,
    )
