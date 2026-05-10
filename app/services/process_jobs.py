import json
import os
import tempfile
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
DEFAULT_HEAVY_JOB_NICE = 5


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


def _heavy_job_nice_increment() -> int:
    try:
        value = int(os.environ.get("MAKERHUB_HEAVY_JOB_NICE") or DEFAULT_HEAVY_JOB_NICE)
    except (TypeError, ValueError):
        return DEFAULT_HEAVY_JOB_NICE
    return max(min(value, 19), 0)


def _apply_heavy_job_niceness() -> None:
    increment = _heavy_job_nice_increment()
    if increment <= 0 or not hasattr(os, "nice"):
        return
    try:
        os.nice(increment)
    except OSError:
        return


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


def _json_safe_payload(payload: Any) -> Any:
    try:
        json.dumps(payload, ensure_ascii=False)
        return payload
    except TypeError:
        return json.loads(json.dumps(payload, ensure_ascii=False, default=str))


def _job_result_path(payload: dict[str, Any]) -> Optional[Path]:
    raw_path = str(payload.get("__job_result_path") or "").strip()
    if not raw_path:
        return None
    try:
        return Path(raw_path)
    except (TypeError, ValueError):
        return None


def _write_job_result_file(payload: dict[str, Any], event_type: str, event_payload: Any) -> bool:
    result_path = _job_result_path(payload)
    if result_path is None:
        return False
    envelope = {
        "type": event_type,
        "payload": _json_safe_payload(event_payload),
    }
    temp_path = result_path.with_name(f"{result_path.name}.{os.getpid()}.tmp")
    try:
        result_path.parent.mkdir(parents=True, exist_ok=True)
        temp_path.write_text(json.dumps(envelope, ensure_ascii=False), encoding="utf-8")
        os.replace(temp_path, result_path)
        return True
    finally:
        try:
            if temp_path.exists():
                temp_path.unlink()
        except OSError:
            pass


def _emit_finished(queue, payload: dict[str, Any], event_type: str, event_payload: Any) -> None:
    try:
        if _write_job_result_file(payload, event_type, event_payload):
            _emit(queue, "finished", {"type": event_type})
            return
    except Exception as exc:
        event_type = "error"
        event_payload = {
            "message": f"后台任务结果写入失败：{exc}",
            "traceback": traceback.format_exc(),
        }
    _emit(queue, event_type, event_payload)


def _read_job_result_file(result_path: Optional[Path]) -> Optional[dict[str, Any]]:
    if result_path is None or not result_path.exists():
        return None
    try:
        envelope = json.loads(result_path.read_text(encoding="utf-8"))
    except Exception:
        return None
    if not isinstance(envelope, dict):
        return None
    event_type = str(envelope.get("type") or "")
    if event_type not in {"result", "error"}:
        return None
    return {"type": event_type, "payload": envelope.get("payload")}


def _run_archive_model_entry(queue, payload: dict[str, Any]) -> None:
    _apply_heavy_job_niceness()

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
            download_comment_assets=payload.get("download_comment_assets"),
            rebuild_archive=bool(payload.get("rebuild_archive", True)),
            record_missing_3mf_log=bool(payload.get("record_missing_3mf_log", True)),
            three_mf_skip_state=str(payload.get("three_mf_skip_state") or ""),
            three_mf_daily_limit_cn=_normalize_three_mf_daily_limit(payload.get("three_mf_daily_limit_cn")),
            three_mf_daily_limit_global=_normalize_three_mf_daily_limit(payload.get("three_mf_daily_limit_global")),
            existing_model_dir=str(payload.get("existing_model_dir") or ""),
        )
        _emit_finished(queue, payload, "result", result)
    except Exception as exc:
        _emit_finished(
            queue,
            payload,
            "error",
            {
                "message": str(exc),
                "traceback": traceback.format_exc(),
            },
        )


def _run_discover_batch_entry(queue, payload: dict[str, Any]) -> None:
    _apply_heavy_job_niceness()

    try:
        result = discover_batch_model_urls(
            str(payload.get("url") or ""),
            str(payload.get("cookie") or ""),
        )
        _emit_finished(queue, payload, "result", result)
    except Exception as exc:
        _emit_finished(
            queue,
            payload,
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
        _emit_finished(queue, payload, "result", {"deleted": bool(result)})
    except Exception as exc:
        _emit_finished(
            queue,
            payload,
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
    result_fd, result_file = tempfile.mkstemp(prefix="makerhub_job_result_", suffix=".json")
    os.close(result_fd)
    result_path = Path(result_file)
    try:
        result_path.unlink()
    except OSError:
        pass
    process_payload = dict(payload)
    process_payload["__job_result_path"] = str(result_path)
    process = JOB_CONTEXT.Process(target=target, args=(queue, process_payload), daemon=True)
    process.start()
    result: Any = None
    error_payload: Optional[dict[str, Any]] = None
    timeout_seconds = int(idle_timeout_seconds or _job_idle_timeout_seconds())
    last_event_at = time.monotonic()

    def handle_message(message: Any) -> bool:
        nonlocal result, error_payload, last_event_at
        if not isinstance(message, dict):
            return False
        last_event_at = time.monotonic()
        event_type = str(message.get("type") or "")
        event_payload = message.get("payload")

        if event_type == "progress":
            if callable(progress_callback) and isinstance(event_payload, dict):
                try:
                    progress_callback(event_payload)
                except Exception:
                    pass
            return False

        if event_type == "result":
            result = event_payload
            return True

        if event_type == "error":
            error_payload = event_payload if isinstance(event_payload, dict) else {"message": str(event_payload or "")}
            return True
        if event_type == "finished":
            file_message = _read_job_result_file(result_path)
            if file_message is not None:
                return handle_message(file_message)
            return False
        return False

    def load_result_file() -> bool:
        file_message = _read_job_result_file(result_path)
        if file_message is None:
            return False
        return handle_message(file_message)

    def drain_final_messages() -> None:
        # A spawned process can exit just before the result event becomes visible
        # on the multiprocessing queue. Drain briefly before treating exit=0 as
        # "no result".
        deadline = time.monotonic() + JOB_EXIT_TIMEOUT_SECONDS
        while result is None and error_payload is None and time.monotonic() < deadline:
            if load_result_file():
                return
            try:
                message = queue.get(timeout=0.2)
            except Empty:
                if not process.is_alive():
                    time.sleep(0.05)
                continue
            if handle_message(message):
                return

    try:
        while True:
            try:
                message = queue.get(timeout=JOB_POLL_SECONDS)
            except Empty:
                if not process.is_alive():
                    drain_final_messages()
                    break
                if time.monotonic() - last_event_at >= timeout_seconds:
                    error_payload = {
                        "message": f"后台任务超过 {timeout_seconds} 秒没有进度，已自动终止。",
                    }
                    break
                continue

            if handle_message(message):
                break
    finally:
        process.join(timeout=JOB_EXIT_TIMEOUT_SECONDS)
        if process.is_alive():
            process.kill()
            process.join(timeout=JOB_EXIT_TIMEOUT_SECONDS)
        if result is None and error_payload is None:
            load_result_file()
        queue.close()
        queue.join_thread()
        try:
            result_path.unlink()
        except OSError:
            pass

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
    download_comment_assets: Optional[bool] = None,
    rebuild_archive: bool = True,
    record_missing_3mf_log: bool = True,
    three_mf_skip_state: str = "",
    three_mf_daily_limit_cn: int = 100,
    three_mf_daily_limit_global: int = 100,
    existing_model_dir: str = "",
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
                download_comment_assets=download_comment_assets,
                rebuild_archive=rebuild_archive,
                record_missing_3mf_log=record_missing_3mf_log,
                three_mf_skip_state=three_mf_skip_state,
                three_mf_daily_limit_cn=three_mf_daily_limit_cn,
                three_mf_daily_limit_global=three_mf_daily_limit_global,
                existing_model_dir=existing_model_dir,
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
                "download_comment_assets": download_comment_assets,
                "rebuild_archive": rebuild_archive,
                "record_missing_3mf_log": record_missing_3mf_log,
                "three_mf_skip_state": three_mf_skip_state,
                "three_mf_daily_limit_cn": three_mf_daily_limit_cn,
                "three_mf_daily_limit_global": three_mf_daily_limit_global,
                "existing_model_dir": existing_model_dir,
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
