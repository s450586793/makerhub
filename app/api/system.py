from __future__ import annotations

import json
import os

from fastapi import APIRouter, HTTPException, Query, Request

from app.api import config as config_api
from app.core.database import database_status
from app.core.settings import APP_VERSION
from app.schemas.models import SystemUpdateRequest
from app.services.request_threads import run_ui_io
from app.services.self_update import get_update_status, request_system_update


router = APIRouter(prefix="/api")


@router.get("/bootstrap")
async def get_bootstrap(request: Request):
    identity = getattr(request.state, "auth_identity", None) or {}
    config = await run_ui_io(config_api.store.load)
    payload = {
        "app_version": APP_VERSION,
        "session": config_api._session_payload(identity, config=config if identity else None),
        "theme_preference": config.user.theme_preference if identity else "",
        "database": await run_ui_io(database_status),
    }
    return config_api._with_version_status(
        payload,
        await config_api._get_github_version_status(proxy_config=config.proxy),
    )


@router.get("/public/makerhub/ping")
async def public_makerhub_ping():
    return {
        "makerhub": True,
        "app_version": APP_VERSION,
    }


@router.get("/config")
async def get_config():
    config = await run_ui_io(config_api.store.load)
    payload = await run_ui_io(config_api._public_config_payload, config)
    return config_api._with_version_status(
        payload,
        await config_api._get_github_version_status(proxy_config=config.proxy),
    )


@router.get("/system/update")
async def get_system_update(force: bool = Query(False)):
    config = await run_ui_io(config_api.store.load)
    payload = await run_ui_io(get_update_status)
    payload = config_api._with_version_status(
        payload,
        await config_api._get_github_version_status(force=force, proxy_config=config.proxy),
    )
    return config_api._with_changelog_status(
        payload,
        await config_api._get_github_changelog_status(force=force, proxy_config=config.proxy),
    )


@router.get("/system/version")
async def get_system_version(force: bool = Query(False)):
    config = await run_ui_io(config_api.store.load)
    payload = {"app_version": APP_VERSION}
    return config_api._with_version_status(
        payload,
        await config_api._get_github_version_status(force=force, proxy_config=config.proxy),
    )


@router.post("/system/update")
async def start_system_update(payload: SystemUpdateRequest, request: Request):
    config_api._require_session_auth(request)
    identity = getattr(request.state, "auth_identity", None) or {}
    requested_by = str(identity.get("username") or "").strip()
    config = config_api.store.load()
    try:
        os.environ["MAKERHUB_RUNTIME_CONFIG_JSON"] = json.dumps(config.runtime.model_dump(), ensure_ascii=False)
        response = request_system_update(
            requested_by=requested_by,
            target_version=str(payload.target_version or ""),
            force=bool(payload.force),
        )
        response = config_api._with_version_status(
            response,
            await config_api._get_github_version_status(proxy_config=config.proxy),
        )
        return config_api._with_changelog_status(
            response,
            await config_api._get_github_changelog_status(proxy_config=config.proxy),
        )
    except RuntimeError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
