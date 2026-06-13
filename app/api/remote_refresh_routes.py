from __future__ import annotations

import os

from fastapi import APIRouter, Request

from app.api.config import _get_github_version_status, _public_config_payload, _require_session_auth, _with_version_status
from app.api.dependencies import remote_refresh_manager, store, task_state_store
from app.schemas.models import RemoteRefreshConfig
from app.services.business_logs import append_business_log
from app.services.catalog import _runtime_snapshot
from app.services.request_threads import run_task_api, run_ui_io


router = APIRouter(prefix="/api")


def _runtime_engine_enabled() -> bool:
    return os.getenv("MAKERHUB_RUNTIME_ENGINE", "").strip().lower() in {"1", "true", "v2", "runtime"}


def _submit_runtime_source_refresh() -> dict:
    from app.api.runtime_routes import runtime_engine
    from app.services.runtime_engine.source_refresh_adapter import SourceRefreshRuntimeAdapter

    runtime_engine.adapters.setdefault("source_refresh", SourceRefreshRuntimeAdapter())
    return runtime_engine.submit_run("source_refresh", {"manual": True})


@router.post("/config/remote-refresh")
async def save_remote_refresh(payload: RemoteRefreshConfig, request: Request):
    _require_session_auth(request)
    config = store.load()
    config.remote_refresh = payload
    store.save(config)
    state = remote_refresh_manager.notify_config_updated()
    append_business_log(
        "settings",
        "remote_refresh_saved",
        "源端刷新设置已保存。",
        enabled=payload.enabled,
        cron=payload.cron,
        next_run_at=state.get("next_run_at"),
    )
    return _with_version_status(_public_config_payload(config), await _get_github_version_status(proxy_config=config.proxy))


@router.get("/remote-refresh")
async def get_remote_refresh_data():
    return await run_ui_io(_source_refresh_payload)


@router.get("/source-refresh")
async def get_source_refresh_data():
    return await run_ui_io(_source_refresh_payload)


def _source_refresh_payload() -> dict:
    config = store.load()
    runtime_source_refresh = _runtime_snapshot("source_refresh")
    return {
        "config": config.remote_refresh.model_dump(),
        "state": remote_refresh_manager.state_payload(),
        "source_refresh": {
            "queue": task_state_store.load_source_refresh_queue(),
            "runs": task_state_store.load_source_refresh_runs(),
        },
        "runtime": {"source_refresh": runtime_source_refresh} if runtime_source_refresh else {},
    }


@router.post("/remote-refresh/run")
async def run_remote_refresh(request: Request):
    _require_session_auth(request)
    return await _trigger_source_refresh_run()


async def _trigger_source_refresh_run():
    if _runtime_engine_enabled():
        return await run_task_api(_submit_runtime_source_refresh)

    def _manual_trigger_payload() -> dict:
        return remote_refresh_manager.trigger_manual_refresh()

    return await run_task_api(_manual_trigger_payload)


@router.post("/source-refresh/run")
async def run_source_refresh(request: Request):
    _require_session_auth(request)
    return await _trigger_source_refresh_run()


@router.post("/source-refresh/repair")
async def repair_source_refresh(request: Request):
    _require_session_auth(request)

    def _repair_payload() -> dict:
        if hasattr(remote_refresh_manager, "repair_source_refresh_state"):
            return remote_refresh_manager.repair_source_refresh_state()
        return {
            "summary": {},
            "queue": task_state_store.load_source_refresh_queue(),
            "runs": task_state_store.load_source_refresh_runs(),
        }

    return await run_task_api(_repair_payload)
