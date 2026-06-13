from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request

from app.api import config as config_api
from app.services.request_threads import run_task_api, run_ui_io
from app.services.runtime_engine.archive_adapter import ArchiveRuntimeAdapter
from app.services.runtime_engine import store
from app.services.runtime_engine.engine import RuntimeEngine
from app.services.runtime_engine.missing_3mf_adapter import Missing3mfRuntimeAdapter
from app.services.runtime_engine.source_refresh_adapter import SourceRefreshRuntimeAdapter
from app.services.runtime_engine.subscription_adapter import SubscriptionRuntimeAdapter


router = APIRouter(prefix="/api")
runtime_engine = RuntimeEngine(
    adapters={
        "archive": ArchiveRuntimeAdapter(),
        "missing_3mf_retry": Missing3mfRuntimeAdapter(),
        "source_refresh": SourceRefreshRuntimeAdapter(),
        "subscription_sync": SubscriptionRuntimeAdapter(),
    }
)


@router.get("/runtime")
async def get_runtime(request: Request):
    config_api._require_session_auth(request)
    return await run_ui_io(runtime_engine.repair)


@router.get("/runtime/runs")
async def get_runtime_runs(request: Request):
    config_api._require_session_auth(request)
    payload = await run_ui_io(runtime_engine.refresh_snapshots)
    return {"success": True, "runs": payload.get("tasks", {}).get("runs", [])}


@router.get("/runtime/runs/{run_id}")
async def get_runtime_run(run_id: str, request: Request):
    config_api._require_session_auth(request)
    runs = store.load_runs()["items"]
    batches = store.load_batches()["items"]
    failures = store.load_failures()["items"]
    run = next((item for item in runs if item.get("run_id") == run_id), None)
    return {
        "success": bool(run),
        "run": run,
        "batches": [item for item in batches if item.get("run_id") == run_id],
        "failure_count": sum(1 for item in failures if item.get("run_id") == run_id),
    }


@router.get("/runtime/runs/{run_id}/failures")
async def get_runtime_run_failures(run_id: str, request: Request, page: int = 1, page_size: int = 50):
    config_api._require_session_auth(request)
    clean_page = max(int(page or 1), 1)
    clean_size = max(1, min(int(page_size or 50), 200))
    failures = [item for item in store.load_failures()["items"] if item.get("run_id") == run_id]
    start = (clean_page - 1) * clean_size
    return {
        "success": True,
        "items": failures[start:start + clean_size],
        "page": clean_page,
        "page_size": clean_size,
        "total": len(failures),
    }


@router.post("/runtime/runs")
async def submit_runtime_run(payload: dict[str, Any], request: Request):
    config_api._require_session_auth(request)
    run_type = str(payload.get("type") or "archive")
    return await run_task_api(runtime_engine.submit_run, run_type, payload)


@router.post("/runtime/runs/{run_id}/pause")
async def pause_runtime_run(run_id: str, request: Request):
    config_api._require_session_auth(request)
    return await run_task_api(runtime_engine.set_run_status, run_id, "paused")


@router.post("/runtime/runs/{run_id}/resume")
async def resume_runtime_run(run_id: str, request: Request):
    config_api._require_session_auth(request)
    return await run_task_api(runtime_engine.set_run_status, run_id, "queued")


@router.post("/runtime/runs/{run_id}/cancel")
async def cancel_runtime_run(run_id: str, request: Request):
    config_api._require_session_auth(request)
    return await run_task_api(runtime_engine.set_run_status, run_id, "cancelled")


@router.post("/runtime/failures/retry")
async def retry_runtime_failures(payload: dict[str, Any], request: Request):
    config_api._require_session_auth(request)
    return await run_task_api(runtime_engine.retry_failures, payload)


@router.post("/runtime/repair")
async def repair_runtime(request: Request):
    config_api._require_session_auth(request)
    return await run_task_api(runtime_engine.repair)
