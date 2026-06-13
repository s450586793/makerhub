from __future__ import annotations

import asyncio
import os
import time
from multiprocessing import Process

from fastapi import APIRouter, HTTPException, Request

from app.api.config import _now_iso, _require_session_auth
from app.api.dependencies import crawler, store, subscription_manager, task_state_store
from app.core.timezone import now_iso as china_now_iso
from app.schemas.models import (
    ArchiveRequest,
    Missing3mfCancelRequest,
    Missing3mfRetryRequest,
    Missing3mfVerificationRetryRequest,
)
from app.services.archive_model_index import archive_model_index_status
from app.services.archive_profile_backfill import read_profile_backfill_status, write_profile_backfill_status
from app.services.archive_repair import read_archive_repair_status, run_archive_repair_job, write_archive_repair_status
from app.services.archive_worker import BATCH_TASK_MODES, detect_archive_mode
from app.services.account_health import mark_account_ok
from app.services.business_logs import append_business_log
from app.services.catalog import build_tasks_payload
from app.services.request_threads import run_task_api, run_ui_io, run_web_io


router = APIRouter(prefix="/api")
archive_repair_process: Process | None = None
archive_repair_start_lock = asyncio.Lock()
profile_backfill_start_lock = asyncio.Lock()


def _runtime_engine_enabled() -> bool:
    return os.getenv("MAKERHUB_RUNTIME_ENGINE", "").strip().lower() in {"1", "true", "v2", "runtime"}


@router.get("/tasks")
async def get_tasks_data():
    def _tasks_payload() -> dict:
        config = store.load()
        fallback_items = [item.model_dump() for item in config.missing_3mf]
        return build_tasks_payload(missing_fallback=fallback_items)

    return await run_ui_io(_tasks_payload)


@router.post("/tasks/recent-failures/clear")
async def clear_recent_archive_failures(request: Request):
    _require_session_auth(request)

    def _clear_payload() -> dict:
        queue = task_state_store.clear_archive_recent_failures()
        config = store.load()
        fallback_items = [item.model_dump() for item in config.missing_3mf]
        payload = build_tasks_payload(missing_fallback=fallback_items)
        payload["cleared_count"] = int(queue.get("cleared_count") or 0)
        payload["message"] = "已清除最近失败记录。"
        payload["success"] = True
        return {
            **payload,
        }

    result = await run_task_api(_clear_payload)
    append_business_log(
        "archive",
        "recent_failures_cleared",
        result.get("message") or "最近失败记录已清除。",
        cleared_count=result.get("cleared_count"),
    )
    return result


@router.post("/tasks/archive-queue/repair")
async def repair_archive_queue(request: Request):
    _require_session_auth(request)

    def _repair_payload() -> dict:
        result = task_state_store.repair_archive_queue()
        return {
            "success": True,
            "message": "队列状态修复完成。",
            "summary": result.get("summary") or {},
            "archive_queue": result.get("queue") or {},
        }

    result = await run_task_api(_repair_payload)
    append_business_log(
        "archive",
        "queue_repair_requested",
        result.get("message") or "队列状态修复完成。",
        **(result.get("summary") or {}),
    )
    return result


@router.post("/tasks/organize/clear")
async def clear_organize_tasks(request: Request):
    _require_session_auth(request)
    cleared = task_state_store.save_organize_tasks({"items": []})
    append_business_log("organizer", "tasks_cleared", "本地整理任务记录已清空。")
    return {
        "success": True,
        "message": "已清空本地整理任务记录。",
        "organize_tasks": cleared,
    }


@router.post("/tasks/missing-3mf/retry")
async def retry_missing_3mf(payload: Missing3mfRetryRequest, request: Request):
    _require_session_auth(request)
    result = await run_task_api(
        crawler.retry_missing_3mf,
        model_url=payload.model_url,
        model_id=payload.model_id,
        source=payload.source,
        title=payload.title,
        instance_id=payload.instance_id,
    )
    append_business_log(
        "missing_3mf",
        "retry_requested",
        result.get("message") or "缺失 3MF 重试已请求。",
        accepted=bool(result.get("accepted")),
        model_id=payload.model_id,
        model_url=payload.model_url,
        instance_id=payload.instance_id,
    )
    return result


@router.post("/tasks/missing-3mf/retry-all")
async def retry_all_missing_3mf(request: Request):
    _require_session_auth(request)
    result = await run_task_api(crawler.retry_all_missing_3mf)
    append_business_log(
        "missing_3mf",
        "retry_all_requested",
        result.get("message") or "缺失 3MF 全部重试已请求。",
        accepted_count=result.get("accepted_count"),
        queued_count=result.get("queued_count"),
        failed_count=result.get("failed_count"),
    )
    return result


@router.post("/tasks/missing-3mf/verification-verified")
async def retry_verified_missing_3mf(payload: Missing3mfVerificationRetryRequest, request: Request):
    _require_session_auth(request)

    def _retry_and_mark_verified() -> dict:
        result = dict(crawler.manager.retry_verification_missing_3mf(platform=payload.platform) or {})
        snapshot = mark_account_ok(
            payload.platform,
            source="manual_verification",
            detail="用户已在 MakerWorld 完成验证，已重新启动同平台 3MF 重试。",
        )
        result["account_health"] = snapshot
        return result

    result = await run_task_api(_retry_and_mark_verified)
    append_business_log(
        "missing_3mf",
        "verification_verified_retry_requested",
        result.get("message") or "验证完成后已请求重试同平台验证类 3MF 任务。",
        platform=payload.platform,
        accepted_count=result.get("accepted_count"),
        queued_count=result.get("queued_count"),
        failed_count=result.get("failed_count"),
        total_count=result.get("total_count"),
    )
    return result


@router.post("/tasks/missing-3mf/cancel")
async def cancel_missing_3mf(payload: Missing3mfCancelRequest, request: Request):
    _require_session_auth(request)
    result = await run_task_api(
        crawler.cancel_missing_3mf,
        model_url=payload.model_url,
        model_id=payload.model_id,
        title=payload.title,
        instance_id=payload.instance_id,
    )
    append_business_log(
        "missing_3mf",
        "cancel_requested",
        result.get("message") or "缺失 3MF 取消已请求。",
        success=bool(result.get("success")),
        removed_count=result.get("removed_count"),
        model_id=payload.model_id,
        model_url=payload.model_url,
        instance_id=payload.instance_id,
    )
    return result


@router.post("/archive")
async def archive_model(payload: ArchiveRequest):
    if _runtime_engine_enabled():
        from app.api.runtime_routes import runtime_engine
        from app.services.runtime_engine.archive_adapter import ArchiveRuntimeAdapter

        runtime_engine.adapters.setdefault("archive", ArchiveRuntimeAdapter())
        return await run_task_api(runtime_engine.submit_run, "archive", {"source_url": payload.url})

    def _archive_model_payload() -> dict:
        batch_preview = None
        archive_mode = detect_archive_mode(payload.url)
        if payload.create_subscription and archive_mode in BATCH_TASK_MODES:
            batch_preview = crawler.manager.peek_batch_preview(
                payload.preview_token,
                payload.url,
                mode=archive_mode,
            )
            if payload.preview_token and batch_preview is None:
                raise HTTPException(status_code=400, detail="预扫描结果已失效，请重新扫描后再确认提交。")

        response = crawler.manager.submit(payload.url, preview_token=payload.preview_token)
        if (
            payload.create_subscription
            and response.get("accepted") is not False
            and archive_mode in BATCH_TASK_MODES
        ):
            try:
                subscription_result = subscription_manager.upsert_from_archive(
                    url=payload.url,
                    mode=archive_mode,
                    discovered_items=list((batch_preview or {}).get("discovered_items") or []),
                    name=str(
                        payload.subscription_name
                        or (batch_preview or {}).get("source_name")
                        or ""
                    ).strip(),
                )
                subscription = subscription_result.get("subscription") or {}
                subscription_name = str(subscription.get("name") or "").strip()
                response["subscription"] = subscription
                response["subscription_created"] = bool(subscription_result.get("created"))
                response["subscription_message"] = (
                    f"已自动创建订阅「{subscription_name}」。"
                    if subscription_result.get("created")
                    else f"已自动更新订阅「{subscription_name}」。"
                )
                response["message"] = (
                    f"{response.get('message') or '归档任务已加入队列。'} "
                    f"{response['subscription_message']}"
                ).strip()
            except Exception as exc:
                response["subscription_error"] = str(exc)
                response["message"] = (
                    f"{response.get('message') or '归档任务已加入队列。'} "
                    f"但订阅写入失败：{exc}"
                ).strip()
                append_business_log(
                    "archive",
                    "archive_subscribe_failed",
                    str(exc),
                    level="error",
                    url=payload.url,
                    mode=archive_mode,
                )
        append_business_log(
            "archive",
            "archive_submitted",
            response.get("message") or "归档提交完成。",
            accepted=bool(response.get("accepted")),
            url=payload.url,
            mode=response.get("mode") or archive_mode,
            task_id=response.get("task_id"),
            create_subscription=payload.create_subscription,
            subscription_created=response.get("subscription_created"),
        )
        return response

    return await run_task_api(_archive_model_payload)


@router.post("/archive/preview")
async def preview_archive_model(payload: ArchiveRequest):
    response = await run_task_api(crawler.preview_archive, payload.url)
    append_business_log(
        "archive",
        "archive_preview",
        response.get("message") or "归档预扫描完成。",
        accepted=bool(response.get("accepted")),
        url=payload.url,
        mode=response.get("mode"),
        discovered_count=response.get("discovered_count"),
        expected_total=response.get("expected_total"),
        queued_count=response.get("queued_count"),
        archived_count=response.get("archived_count"),
        new_count=response.get("new_count"),
    )
    return response


@router.get("/admin/archive/repair-3mf")
async def get_archive_3mf_repair_status(request: Request):
    _require_session_auth(request)
    return await run_ui_io(read_archive_repair_status)


@router.post("/admin/archive/repair-3mf")
async def repair_archive_3mf(request: Request):
    global archive_repair_process

    _require_session_auth(request)
    async with archive_repair_start_lock:
        state = read_archive_repair_status()
        if state.get("running"):
            state.update(
                {
                    "accepted": False,
                    "message": "全库 3MF 映射修复正在后台执行，请稍后查看状态或日志。",
                }
            )
            return state

        run_id = f"repair-{int(time.time() * 1000)}"
        started_at = _now_iso()
        process = Process(
            target=run_archive_repair_job,
            args=(run_id, started_at),
            daemon=True,
        )
        process.start()
        archive_repair_process = process
        state = write_archive_repair_status(
            {
                "running": True,
                "started_at": started_at,
                "finished_at": "",
                "last_error": "",
                "run_id": run_id,
                "pid": int(process.pid or 0),
                "last_result": {},
            }
        )

    append_business_log(
        "archive_repair",
        "repair_requested",
        "已提交全库 3MF 映射修复，后台开始执行。",
        run_id=run_id,
    )
    state.update(
        {
            "accepted": True,
            "message": "修复任务已提交到后台，请稍后查看日志或状态接口。",
        }
    )
    return state


@router.get("/admin/archive/profile-backfill")
async def get_archive_profile_backfill_status(request: Request):
    _require_session_auth(request)
    status = await run_ui_io(read_profile_backfill_status)
    return _compact_profile_backfill_status(status)


@router.post("/admin/archive/profile-backfill")
async def start_archive_profile_backfill(request: Request):
    _require_session_auth(request)
    async with profile_backfill_start_lock:
        state = read_profile_backfill_status()
        if state.get("running"):
            state.update(
                {
                    "accepted": False,
                    "message": "现有库信息补全正在扫描并持续入队，请稍后刷新状态。",
                }
            )
            return _compact_profile_backfill_status(state)

        started_at = china_now_iso()
        state = write_profile_backfill_status(
            {
                "running": True,
                "phase": "database_migration",
                "database_rebuild_requested": True,
                "force_database_rebuild": True,
                "database_only": False,
                "auto_database_migration": False,
                "started_at": started_at,
                "finished_at": "",
                "last_error": "",
                "last_result": {},
            }
        )

    state.update(
        {
            "accepted": True,
            "message": "数据库索引重建已提交，后台 worker 会先遍历历史库刷新索引，再继续检查缺失信息。",
        }
    )
    return _compact_profile_backfill_status(state)


def _compact_profile_backfill_status(status: dict) -> dict:
    payload = dict(status or {})
    try:
        payload["database"] = archive_model_index_status()
    except Exception:
        payload["database"] = {}
    result = payload.get("last_result")
    if isinstance(result, dict):
        compact_result = dict(result)
        items = compact_result.get("items")
        if isinstance(items, list) and len(items) > 50:
            compact_result["items"] = items[:50]
            compact_result["items_total"] = len(items)
            compact_result["items_truncated"] = True
        payload["last_result"] = compact_result
    return payload
