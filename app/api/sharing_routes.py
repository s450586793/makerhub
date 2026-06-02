from __future__ import annotations

from datetime import timedelta

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import FileResponse

from app.api import config as config_api
from app.api.dependencies import store
from app.core.settings import ARCHIVE_DIR
from app.core.timezone import now as china_now, parse_datetime
from app.schemas.models import ShareCreateRequest, ShareDeleteExpiredRequest, ShareReceiveRequest, SharingConfig
from app.services.business_logs import append_business_log
from app.services.request_threads import run_task_api, run_ui_io


router = APIRouter(prefix="/api")


@router.get("/public/shares/{share_id}/manifest")
async def public_share_manifest(share_id: str, token: str = Query("")):
    try:
        record = await run_ui_io(config_api._validate_share_record, share_id, token)
        return await run_ui_io(config_api._manifest_from_record, record, token)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get("/public/share-access/{access_code}/manifest")
async def public_share_access_manifest(access_code: str):
    try:
        record = await run_ui_io(config_api._validate_share_record_by_access_code, access_code)
        return await run_ui_io(config_api._manifest_from_record, record, "", access_code)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get("/public/shares/{share_id}/files/{file_id}")
async def public_share_file(share_id: str, file_id: str, token: str = Query(""), access: str = Query("")):
    try:
        if str(access or "").strip():
            record = await run_ui_io(config_api._validate_share_record_by_access_code, access)
        else:
            record = await run_ui_io(config_api._validate_share_record, share_id, token)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    if str(record.get("id") or "") != str(share_id or ""):
        raise HTTPException(status_code=404, detail="分享文件不存在。")
    file_item = next(
        (
            item for item in record.get("files") or []
            if isinstance(item, dict) and str(item.get("id") or "") == str(file_id or "")
        ),
        None,
    )
    if not file_item:
        raise HTTPException(status_code=404, detail="分享文件不存在。")
    source_model_dir = str(file_item.get("source_model_dir") or "").strip().strip("/")
    rel_path = str(file_item.get("rel_path") or "").strip().lstrip("/")
    target = (ARCHIVE_DIR / source_model_dir / rel_path).resolve()
    try:
        target.relative_to((ARCHIVE_DIR / source_model_dir).resolve())
    except ValueError as exc:
        raise HTTPException(status_code=404, detail="分享文件路径无效。") from exc
    if not target.exists() or not target.is_file():
        raise HTTPException(status_code=404, detail="分享文件不存在。")
    return FileResponse(
        target,
        media_type=str(file_item.get("mime_type") or "application/octet-stream"),
        filename=str(file_item.get("name") or target.name),
    )


@router.post("/config/sharing")
async def save_sharing(payload: SharingConfig, request: Request):
    config_api._require_session_auth(request)
    config = store.load()
    try:
        normalized_url = config_api._normalize_public_base_url(payload.public_base_url)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    options = config_api._normalize_share_options(payload)
    config.sharing = SharingConfig(
        public_base_url=normalized_url,
        default_expires_days=options["expires_days"],
        include_images=options["include_images"],
        include_model_files=options["include_model_files"],
        model_file_types=options["model_file_types"],
        include_attachments=options["include_attachments"],
        attachment_file_types=options["attachment_file_types"],
        include_comments=options["include_comments"],
    )
    append_business_log(
        "settings",
        "sharing_saved",
        "模型分享配置已保存。",
        public_base_url=normalized_url,
    )
    saved = store.save(config)
    return config_api._with_version_status(
        config_api._public_config_payload(saved),
        await config_api._get_github_version_status(proxy_config=saved.proxy),
    )


@router.post("/config/sharing/test")
async def test_sharing(payload: SharingConfig, request: Request):
    config_api._require_session_auth(request)
    try:
        result = await run_task_api(config_api._run_public_base_url_test, payload.public_base_url)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    append_business_log(
        "settings",
        "sharing_public_url_tested",
        result.get("message") or "模型分享公开访问地址检测完成。",
        ok=bool(result.get("ok")),
        public_base_url=result.get("base_url") or payload.public_base_url,
        ping_url=result.get("ping_url") or "",
        status_code=int(result.get("status_code") or 0),
        elapsed_ms=result.get("elapsed_ms"),
    )
    return result


@router.get("/config/sharing/check")
async def check_sharing_public_url(request: Request):
    config_api._require_session_auth(request)
    config = await run_ui_io(store.load)
    try:
        return await run_task_api(config_api._run_public_base_url_test, config.sharing.public_base_url)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/sharing/create")
async def create_model_share(payload: ShareCreateRequest, request: Request):
    config_api._require_session_auth(request)

    def _create_payload() -> dict:
        config = store.load()
        base_url = config_api._normalize_public_base_url(config.sharing.public_base_url)
        if not base_url:
            raise ValueError("请先到设置 -> 模型分享 配置公开访问地址。")
        config_api._run_public_base_url_test(base_url)
        options = config_api._normalize_share_options(payload.options)
        model_dirs = []
        seen_dirs = set()
        for item in payload.model_dirs or []:
            clean_item = str(item or "").strip().strip("/")
            if clean_item and clean_item not in seen_dirs:
                model_dirs.append(clean_item)
                seen_dirs.add(clean_item)
        if not model_dirs:
            raise ValueError("请选择要分享的模型。")
        state = config_api._read_shares_state()
        share_id = config_api._new_share_id()
        token = config_api._new_share_token()
        now_dt = china_now()
        state["items"] = [
            item for item in state.get("items") or []
            if isinstance(item, dict) and parse_datetime(item.get("expires_at")) not in (None,)
            and parse_datetime(item.get("expires_at")) >= now_dt
        ]
        conflicts = config_api._active_share_model_conflicts(state, model_dirs, now_dt)
        if conflicts:
            conflict_message = config_api._active_share_conflict_message(conflicts)
            append_business_log(
                "sharing",
                "share_duplicate_blocked",
                conflict_message,
                level="warning",
                model_dirs=model_dirs,
                conflict_count=len(conflicts),
                share_ids=[item.get("share_id") or "" for item in conflicts],
            )
            raise ValueError(conflict_message)
        expires_at = now_dt + timedelta(days=options["expires_days"])
        files: list[dict] = []
        path_to_id: dict[str, str] = {}
        models = [
            config_api._build_share_model_entry(
                model_dir=model_dir,
                options=options,
                files=files,
                path_to_id=path_to_id,
            )
            for model_dir in model_dirs
        ]
        record = {
            "id": share_id,
            "token": token,
            "token_hash": config_api._share_token_hash(token),
            "created_at": now_dt.isoformat(timespec="seconds"),
            "expires_at": expires_at.isoformat(timespec="seconds"),
            "options": options,
            "models": models,
            "files": files,
        }
        access_code, _ = config_api._ensure_share_access_code(record)
        state["items"].append(record)
        config_api._write_shares_state(state)
        share_code = config_api._encode_share_code(base_url=base_url, access_code=access_code)
        return {
            "success": True,
            "share_id": share_id,
            "share_code": share_code,
            "expires_at": record["expires_at"],
            "model_count": len(models),
            "file_count": len(files),
            "file_counts": config_api._share_file_counts(files),
            "message": f"已生成 {len(models)} 个模型的分享码。",
        }

    try:
        result = await run_task_api(_create_payload)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    append_business_log(
        "sharing",
        "share_created",
        result.get("message") or "模型分享码已生成。",
        share_id=result.get("share_id") or "",
        model_count=int(result.get("model_count") or 0),
        file_count=int(result.get("file_count") or 0),
    )
    return result


@router.get("/sharing/shares")
async def list_model_shares(request: Request):
    config_api._require_session_auth(request)

    def _list_payload() -> dict:
        config = store.load()
        base_url = config_api._normalize_public_base_url(config.sharing.public_base_url)
        state = config_api._read_shares_state()
        items = [
            config_api._share_record_summary(item, base_url=base_url)
            for item in state.get("items") or []
            if isinstance(item, dict)
        ]
        items.sort(key=lambda item: item.get("created_at") or "", reverse=True)
        return {
            "success": True,
            "items": items,
            "count": len(items),
        }

    return await run_ui_io(_list_payload)


@router.post("/sharing/shares/{share_id}/code")
async def ensure_model_share_code(share_id: str, request: Request):
    config_api._require_session_auth(request)

    def _code_payload() -> dict:
        config = store.load()
        base_url = config_api._normalize_public_base_url(config.sharing.public_base_url)
        if not base_url:
            raise ValueError("请先到设置 -> 模型分享 配置公开访问地址。")
        clean_id = str(share_id or "").strip()
        state = config_api._read_shares_state()
        items = [item for item in state.get("items") or [] if isinstance(item, dict)]
        target = next((item for item in items if str(item.get("id") or "") == clean_id), None)
        if not target:
            raise ValueError("分享记录不存在。")
        expires_at = parse_datetime(target.get("expires_at"))
        if expires_at is not None and expires_at < china_now():
            raise ValueError("分享已过期，请清理或重新分享。")
        token = str(target.get("token") or "").strip()
        generated = False
        if not token:
            token = config_api._new_share_token()
            target["token"] = token
            target["token_hash"] = config_api._share_token_hash(token)
            generated = True
        access_code, access_generated = config_api._ensure_share_access_code(target)
        generated = generated or access_generated
        if generated:
            config_api._write_shares_state({"items": items})
        share_code = config_api._encode_share_code(base_url=base_url, access_code=access_code)
        return {
            "success": True,
            "share_id": clean_id,
            "share_code": share_code,
            "generated": generated,
            "message": "分享码已生成。" if generated else "分享码已读取。",
        }

    try:
        result = await run_ui_io(_code_payload)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    append_business_log(
        "sharing",
        "share_code_generated" if result.get("generated") else "share_code_accessed",
        result.get("message") or "分享码已处理。",
        share_id=result.get("share_id") or "",
    )
    return result


@router.delete("/sharing/shares/{share_id}")
async def revoke_model_share(share_id: str, request: Request):
    config_api._require_session_auth(request)

    def _delete_payload() -> dict:
        clean_id = str(share_id or "").strip()
        state = config_api._read_shares_state()
        items = [item for item in state.get("items") or [] if isinstance(item, dict)]
        next_items = [item for item in items if str(item.get("id") or "") != clean_id]
        if len(next_items) == len(items):
            raise ValueError("分享记录不存在。")
        state["items"] = next_items
        config_api._write_shares_state(state)
        return {
            "success": True,
            "message": "分享已撤销。",
            "items": [config_api._share_record_summary(item) for item in next_items],
            "count": len(next_items),
        }

    try:
        result = await run_ui_io(_delete_payload)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    append_business_log("sharing", "share_revoked", "模型分享已撤销。", share_id=share_id)
    return result


@router.post("/sharing/shares/cleanup")
async def cleanup_model_shares(payload: ShareDeleteExpiredRequest, request: Request):
    config_api._require_session_auth(request)

    def _cleanup_payload() -> dict:
        if not payload.include_expired:
            raise ValueError("请选择要清理的分享记录。")
        state = config_api._read_shares_state()
        now_dt = china_now()
        items = [item for item in state.get("items") or [] if isinstance(item, dict)]
        next_items = []
        removed_count = 0
        for item in items:
            expires_at = parse_datetime(item.get("expires_at"))
            if expires_at is not None and expires_at < now_dt:
                removed_count += 1
                continue
            next_items.append(item)
        state["items"] = next_items
        config_api._write_shares_state(state)
        return {
            "success": True,
            "message": f"已清理 {removed_count} 条过期分享。",
            "removed_count": removed_count,
            "items": [config_api._share_record_summary(item) for item in next_items],
            "count": len(next_items),
        }

    try:
        result = await run_ui_io(_cleanup_payload)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    append_business_log(
        "sharing",
        "share_expired_cleaned",
        result.get("message") or "过期分享已清理。",
        removed_count=int(result.get("removed_count") or 0),
    )
    return result


@router.post("/sharing/receive/preview")
async def preview_model_share(payload: ShareReceiveRequest, request: Request):
    config_api._require_session_auth(request)

    def _preview_payload() -> dict:
        decoded, manifest = config_api._fetch_share_manifest(payload.share_code)
        config_api._validate_share_manifest_limits(manifest)
        duplicates = config_api._find_manifest_duplicates(manifest)
        models = manifest.get("models") if isinstance(manifest.get("models"), list) else []
        files = manifest.get("files") if isinstance(manifest.get("files"), list) else []
        file_counts = config_api._share_file_counts(files)
        return {
            "success": True,
            "can_import": not duplicates,
            "duplicate_count": len(duplicates),
            "duplicates": duplicates,
            "manifest": {
                "share_id": str(manifest.get("share_id") or decoded.get("share_id") or ""),
                "created_at": str(manifest.get("created_at") or ""),
                "expires_at": str(manifest.get("expires_at") or ""),
                "model_count": len(models),
                "file_count": len(files),
                "file_counts": file_counts,
                "models": [
                    {
                        "title": str(item.get("title") or (item.get("meta") or {}).get("title") or ""),
                        "id": str(item.get("id") or ""),
                        "origin_url": str(item.get("origin_url") or ""),
                        "file_count": config_api._share_file_counts(files, str(item.get("model_dir") or "")).get("total", 0),
                        "model_file_count": config_api._share_file_counts(files, str(item.get("model_dir") or "")).get("model", 0),
                        "image_count": config_api._share_file_counts(files, str(item.get("model_dir") or "")).get("image", 0),
                        "attachment_count": config_api._share_file_counts(files, str(item.get("model_dir") or "")).get("attachment", 0),
                    }
                    for item in models
                    if isinstance(item, dict)
                ],
            },
            "message": "分享可导入。" if not duplicates else "本地已存在分享中的模型，不能重复导入。",
        }

    try:
        return await run_task_api(_preview_payload)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/sharing/receive/import")
async def import_model_share(payload: ShareReceiveRequest, request: Request):
    config_api._require_session_auth(request)

    def _import_payload() -> dict:
        decoded, manifest = config_api._fetch_share_manifest(payload.share_code)
        config_api._validate_share_manifest_limits(manifest)
        return config_api._import_share_manifest(decoded=decoded, manifest=manifest)

    try:
        result = await run_task_api(_import_payload)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    append_business_log(
        "sharing",
        "share_imported" if result.get("success") else "share_import_skipped_duplicate",
        result.get("message") or "分享导入已处理。",
        imported_count=len(result.get("imported") or []),
        duplicate_count=len(result.get("duplicates") or []),
    )
    return result
