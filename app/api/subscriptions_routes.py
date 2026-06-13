from __future__ import annotations

import os

from fastapi import APIRouter, HTTPException, Query, Request

from app.api.config import _get_github_version_status, _public_config_payload, _require_session_auth, _with_version_status
from app.api.dependencies import store, subscription_manager
from app.schemas.models import SubscriptionCreateRequest, SubscriptionSettingsUpdate, SubscriptionUpdateRequest
from app.services.business_logs import append_business_log
from app.services.catalog import _runtime_snapshot
from app.services.request_threads import run_task_api, run_web_io


router = APIRouter(prefix="/api")


def _runtime_engine_enabled() -> bool:
    return os.getenv("MAKERHUB_RUNTIME_ENGINE", "").strip().lower() in {"1", "true", "v2", "runtime"}


def _submit_runtime_subscription_sync(subscription_id: str) -> dict:
    from app.api.runtime_routes import runtime_engine
    from app.services.runtime_engine.subscription_adapter import SubscriptionRuntimeAdapter

    runtime_engine.adapters.setdefault("subscription_sync", SubscriptionRuntimeAdapter())
    return runtime_engine.submit_run("subscription_sync", {"source_id": subscription_id})


@router.post("/config/subscriptions")
async def save_subscription_settings(payload: SubscriptionSettingsUpdate, request: Request):
    _require_session_auth(request)
    config = store.load()
    config.subscription_settings = payload
    store.save(config)
    append_business_log(
        "settings",
        "subscription_settings_saved",
        "订阅设置已保存。",
        default_cron=payload.default_cron,
        default_enabled=payload.default_enabled,
        default_initialize_from_source=payload.default_initialize_from_source,
        card_sort=payload.card_sort,
        hide_disabled_from_cards=payload.hide_disabled_from_cards,
    )
    return _with_version_status(_public_config_payload(config), await _get_github_version_status(proxy_config=config.proxy))


@router.get("/subscriptions")
async def get_subscriptions_data(
    page: int = Query(1, ge=1, description="订阅来源分页页码"),
    page_size: int = Query(8, ge=1, le=120, description="每页订阅来源数量"),
    limit: int = Query(0, ge=0, le=2000, description="从第一页起一次返回的数量"),
):
    def _payload() -> dict:
        payload = subscription_manager.list_payload(page=page, page_size=page_size, limit=limit)
        runtime_subscriptions = _runtime_snapshot("subscriptions")
        if runtime_subscriptions:
            payload["runtime"] = {"subscriptions": runtime_subscriptions}
        return payload

    return await run_web_io(_payload)


@router.post("/subscriptions")
async def create_subscription(payload: SubscriptionCreateRequest, request: Request):
    _require_session_auth(request)
    try:
        result = await run_task_api(
            subscription_manager.create_subscription,
            url=payload.url,
            cron=payload.cron,
            name=payload.name,
            enabled=payload.enabled,
            initialize_from_source=payload.initialize_from_source,
        )
        append_business_log(
            "subscription",
            "created",
            result.get("message") or "订阅已创建。",
            url=payload.url,
            name=payload.name,
            enabled=payload.enabled,
            initialize_from_source=payload.initialize_from_source,
        )
        return result
    except (ValueError, RuntimeError) as exc:
        append_business_log("subscription", "create_failed", str(exc), level="error", url=payload.url, name=payload.name)
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.put("/subscriptions/{subscription_id}")
async def update_subscription(subscription_id: str, payload: SubscriptionUpdateRequest, request: Request):
    _require_session_auth(request)
    try:
        result = await run_task_api(
            subscription_manager.update_subscription,
            subscription_id,
            url=payload.url,
            name=payload.name,
            cron=payload.cron,
            enabled=payload.enabled,
        )
        append_business_log(
            "subscription",
            "updated",
            result.get("message") or "订阅已更新。",
            subscription_id=subscription_id,
            url=payload.url,
            name=payload.name,
            enabled=payload.enabled,
        )
        return result
    except (ValueError, RuntimeError) as exc:
        append_business_log(
            "subscription",
            "update_failed",
            str(exc),
            level="error",
            subscription_id=subscription_id,
            url=payload.url,
        )
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.delete("/subscriptions/{subscription_id}")
async def delete_subscription(subscription_id: str, request: Request):
    _require_session_auth(request)
    try:
        result = await run_task_api(subscription_manager.delete_subscription, subscription_id)
        append_business_log(
            "subscription",
            "deleted",
            result.get("message") or "订阅已删除。",
            subscription_id=subscription_id,
        )
        return result
    except (ValueError, RuntimeError) as exc:
        append_business_log("subscription", "delete_failed", str(exc), level="error", subscription_id=subscription_id)
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/subscriptions/{subscription_id}/sync")
async def sync_subscription(subscription_id: str, request: Request):
    _require_session_auth(request)
    try:
        if _runtime_engine_enabled():
            result = await run_task_api(_submit_runtime_subscription_sync, subscription_id)
            append_business_log(
                "subscription",
                "sync_requested",
                result.get("message") or "订阅同步已提交运行核心。",
                subscription_id=subscription_id,
                runtime_engine=True,
            )
            return result

        result = await run_task_api(subscription_manager.request_sync, subscription_id)
        append_business_log(
            "subscription",
            "sync_requested",
            result.get("message") or "订阅同步已触发。",
            subscription_id=subscription_id,
        )
        return result
    except (ValueError, RuntimeError) as exc:
        append_business_log("subscription", "sync_request_failed", str(exc), level="error", subscription_id=subscription_id)
        raise HTTPException(status_code=400, detail=str(exc)) from exc
