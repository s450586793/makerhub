import os
import time
import traceback
from multiprocessing import get_context
from pathlib import Path
from queue import Empty
from typing import Any, Callable, Optional

import requests

from app.services.batch_discovery import discover_batch_model_urls, normalize_source_url
from app.services.cookie_utils import sanitize_cookie_header
from app.services.legacy_archiver import archive_model as legacy_archive_model
from app.services.resource_limiter import resource_slot


JOB_CONTEXT = get_context("spawn")
JOB_POLL_SECONDS = 0.5
JOB_EXIT_TIMEOUT_SECONDS = 5
DEFAULT_JOB_IDLE_TIMEOUT_SECONDS = 30 * 60
DEFAULT_THREE_MF_DAILY_LIMIT = 100


def _normalize_three_mf_daily_limit(value: Any, fallback: int = DEFAULT_THREE_MF_DAILY_LIMIT) -> int:
    try:
        limit = int(value)
    except (TypeError, ValueError):
        return fallback
    return max(0, limit)


def _job_mode() -> str:
    return str(os.environ.get("MAKERHUB_HEAVY_JOB_MODE") or "process").strip().lower() or "process"


def _use_subprocess() -> bool:
    return _job_mode() != "inline"


def _job_idle_timeout_seconds() -> int:
    try:
        value = int(os.environ.get("MAKERHUB_HEAVY_JOB_IDLE_TIMEOUT_SECONDS") or DEFAULT_JOB_IDLE_TIMEOUT_SECONDS)
    except (TypeError, ValueError):
        return DEFAULT_JOB_IDLE_TIMEOUT_SECONDS
    return max(value, 30)


def _source_looks_deleted(url: str, cookie: str) -> bool:
    cookie_header = sanitize_cookie_header(cookie)
    headers = {
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "User-Agent": "Mozilla/5.0 (Makerhub Worker)",
    }
    if cookie_header:
        headers["Cookie"] = cookie_header
    try:
        response = requests.get(
            normalize_source_url(url),
            headers=headers,
            timeout=(6, 12),
            allow_redirects=True,
        )
    except Exception:
        return False
    if response.status_code == 404:
        return True
    final_url = str(response.url or "")
    return "/404" in final_url or "not found" in response.text[:400].lower()


def _emit(queue, event_type: str, payload: Any) -> None:
    try:
        queue.put({"type": event_type, "payload": payload})
    except Exception:
        pass


def _run_archive_model_entry(queue, payload: dict[str, Any]) -> None:
    def progress_callback(progress_payload: dict[str, Any]) -> None:
        _emit(queue, "progress", progress_payload)

    try:
        result = legacy_archive_model(
            url=str(payload.get("url") or ""),
            cookie=str(payload.get("cookie") or ""),
            download_dir=Path(str(payload.get("download_dir") or "")),
            logs_dir=Path(str(payload.get("logs_dir") or "")),
            existing_root=Path(str(payload.get("existing_root") or "")) if str(payload.get("existing_root") or "").strip() else None,
            progress_callback=progress_callback,
            skip_three_mf_fetch=bool(payload.get("skip_three_mf_fetch")),
            three_mf_skip_message=str(payload.get("three_mf_skip_message") or ""),
            profile_metadata_only=bool(payload.get("profile_metadata_only")),
            download_assets=bool(payload.get("download_assets", True)),
            rebuild_archive=bool(payload.get("rebuild_archive", True)),
            record_missing_3mf_log=bool(payload.get("record_missing_3mf_log", True)),
            three_mf_skip_state=str(payload.get("three_mf_skip_state") or ""),
            three_mf_daily_limit_cn=_normalize_three_mf_daily_limit(payload.get("three_mf_daily_limit_cn")),
            three_mf_daily_limit_global=_normalize_three_mf_daily_limit(payload.get("three_mf_daily_limit_global")),
        )
        _emit(queue, "result", result)
    except Exception as exc:
        _emit(
            queue,
            "error",
            {
                "message": str(exc),
                "traceback": traceback.format_exc(),
            },
        )


def _run_discover_batch_entry(queue, payload: dict[str, Any]) -> None:
    try:
        result = discover_batch_model_urls(
            str(payload.get("url") or ""),
            str(payload.get("cookie") or ""),
        )
        _emit(queue, "result", result)
    except Exception as exc:
        _emit(
            queue,
            "error",
            {
                "message": str(exc),
                "traceback": traceback.format_exc(),
            },
        )


def _run_source_deleted_entry(queue, payload: dict[str, Any]) -> None:
    try:
        result = _source_looks_deleted(
            str(payload.get("url") or ""),
            str(payload.get("cookie") or ""),
        )
        _emit(queue, "result", {"deleted": bool(result)})
    except Exception as exc:
        _emit(
            queue,
            "error",
            {
                "message": str(exc),
                "traceback": traceback.format_exc(),
            },
        )


def _run_process_job(
    target: Callable[..., None],
    payload: dict[str, Any],
    *,
    progress_callback: Optional[Callable[[dict[str, Any]], None]] = None,
    idle_timeout_seconds: Optional[int] = None,
) -> Any:
    queue = JOB_CONTEXT.Queue()
    process = JOB_CONTEXT.Process(target=target, args=(queue, payload), daemon=True)
    process.start()
    result: Any = None
    error_payload: Optional[dict[str, Any]] = None
    timeout_seconds = int(idle_timeout_seconds or _job_idle_timeout_seconds())
    last_event_at = time.monotonic()

    try:
        while True:
            try:
                message = queue.get(timeout=JOB_POLL_SECONDS)
            except Empty:
                if not process.is_alive():
                    break
                if time.monotonic() - last_event_at >= timeout_seconds:
                    error_payload = {
                        "message": f"后台任务超过 {timeout_seconds} 秒没有进度，已自动终止。",
                    }
                    break
                continue

            last_event_at = time.monotonic()
            event_type = str(message.get("type") or "")
            event_payload = message.get("payload")

            if event_type == "progress":
                if callable(progress_callback) and isinstance(event_payload, dict):
                    try:
                        progress_callback(event_payload)
                    except Exception:
                        pass
                continue

            if event_type == "result":
                result = event_payload
                break

            if event_type == "error":
                error_payload = event_payload if isinstance(event_payload, dict) else {"message": str(event_payload or "")}
                break
    finally:
        process.join(timeout=JOB_EXIT_TIMEOUT_SECONDS)
        if process.is_alive():
            process.kill()
            process.join(timeout=JOB_EXIT_TIMEOUT_SECONDS)
        queue.close()
        queue.join_thread()

    if error_payload is not None:
        message = str(error_payload.get("message") or "后台任务执行失败。").strip() or "后台任务执行失败。"
        raise RuntimeError(message)
    if result is None:
        if process.exitcode not in (0, None):
            raise RuntimeError(f"后台任务进程异常退出（exit={process.exitcode}）。")
        raise RuntimeError("后台任务未返回结果。")
    return result


def run_archive_model_job(
    *,
    url: str,
    cookie: str,
    download_dir: str,
    logs_dir: str,
    existing_root: str = "",
    progress_callback: Optional[Callable[[dict[str, Any]], None]] = None,
    skip_three_mf_fetch: bool = False,
    three_mf_skip_message: str = "",
    profile_metadata_only: bool = False,
    download_assets: bool = True,
    rebuild_archive: bool = True,
    record_missing_3mf_log: bool = True,
    three_mf_skip_state: str = "",
    three_mf_daily_limit_cn: int = 100,
    three_mf_daily_limit_global: int = 100,
) -> dict[str, Any]:
    with resource_slot("makerworld_page_api", detail=normalize_source_url(url)):
        if not _use_subprocess():
            return legacy_archive_model(
                url=url,
                cookie=cookie,
                download_dir=Path(download_dir),
                logs_dir=Path(logs_dir),
                existing_root=Path(existing_root) if str(existing_root or "").strip() else None,
                progress_callback=progress_callback,
                skip_three_mf_fetch=skip_three_mf_fetch,
                three_mf_skip_message=three_mf_skip_message,
                profile_metadata_only=profile_metadata_only,
                download_assets=download_assets,
                rebuild_archive=rebuild_archive,
                record_missing_3mf_log=record_missing_3mf_log,
                three_mf_skip_state=three_mf_skip_state,
                three_mf_daily_limit_cn=three_mf_daily_limit_cn,
                three_mf_daily_limit_global=three_mf_daily_limit_global,
            )
        return _run_process_job(
            _run_archive_model_entry,
            {
                "url": url,
                "cookie": cookie,
                "download_dir": download_dir,
                "logs_dir": logs_dir,
                "existing_root": existing_root,
                "skip_three_mf_fetch": skip_three_mf_fetch,
                "three_mf_skip_message": three_mf_skip_message,
                "profile_metadata_only": profile_metadata_only,
                "download_assets": download_assets,
                "rebuild_archive": rebuild_archive,
                "record_missing_3mf_log": record_missing_3mf_log,
                "three_mf_skip_state": three_mf_skip_state,
                "three_mf_daily_limit_cn": three_mf_daily_limit_cn,
                "three_mf_daily_limit_global": three_mf_daily_limit_global,
            },
            progress_callback=progress_callback,
        )


def run_discover_batch_urls_job(url: str, cookie: str) -> dict[str, Any]:
    with resource_slot("makerworld_page_api", detail=normalize_source_url(url)):
        if not _use_subprocess():
            return discover_batch_model_urls(url, cookie)
        return _run_process_job(
            _run_discover_batch_entry,
            {
                "url": url,
                "cookie": cookie,
            },
        )


def run_source_deleted_check_job(url: str, cookie: str) -> bool:
    with resource_slot("makerworld_page_api", detail=normalize_source_url(url)):
        if not _use_subprocess():
            return _source_looks_deleted(url, cookie)
        result = _run_process_job(
            _run_source_deleted_entry,
            {
                "url": url,
                "cookie": cookie,
            },
        )
        return bool(result.get("deleted")) if isinstance(result, dict) else bool(result)
