from __future__ import annotations

import json
import os
import subprocess
import tempfile
from pathlib import Path
from typing import Any

from app.core.settings import (
    ARCHIVE_DIR,
    LOCAL_PREVIEW_MAX_BYTES,
    LOCAL_PREVIEW_TIMEOUT_SECONDS,
    ROOT_DIR,
)
from app.core.timezone import now_iso as china_now_iso
from app.services.business_logs import append_business_log
from app.services.catalog import invalidate_archive_snapshot, invalidate_model_detail_cache, upsert_archive_snapshot_model
from app.services.local_model_preview import (
    PREVIEW_VERSION,
    apply_generated_preview_image,
    build_local_preview_state,
    first_previewable_instance,
    record_generated_preview_failure,
)


SUPPORTED_PREVIEW_SUFFIXES = {".3mf", ".obj", ".stl"}
RENDERER_SCRIPT = ROOT_DIR / "app" / "services" / "local_preview_renderer.mjs"
DEFAULT_RENDER_SIZE = 720


def _read_meta(meta_path: Path) -> dict[str, Any] | None:
    try:
        payload = json.loads(meta_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def _write_meta(model_root: Path, meta: dict[str, Any], *, reason: str) -> None:
    meta_path = model_root / "meta.json"
    meta["update_time"] = china_now_iso()
    temp_path = meta_path.with_name(f".{meta_path.name}.{os.getpid()}.tmp")
    temp_path.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    temp_path.replace(meta_path)
    try:
        model_dir = model_root.resolve().relative_to(ARCHIVE_DIR.resolve()).as_posix()
    except ValueError:
        model_dir = model_root.name
    invalidate_model_detail_cache(model_dir)
    if not upsert_archive_snapshot_model(model_dir, reason=reason):
        invalidate_archive_snapshot(reason)


def _model_dir(model_root: Path) -> str:
    try:
        return model_root.resolve().relative_to(ARCHIVE_DIR.resolve()).as_posix()
    except ValueError:
        return model_root.name


def _status(meta: dict[str, Any]) -> str:
    local_import = meta.get("localImport") if isinstance(meta.get("localImport"), dict) else {}
    return str(local_import.get("previewStatus") or "").strip().lower()


def _mark_preview_pending(meta: dict[str, Any], candidate: dict[str, Any]) -> None:
    local_import = meta.get("localImport") if isinstance(meta.get("localImport"), dict) else {}
    local_import.update(
        {
            "previewGenerator": "three",
            "previewVersion": PREVIEW_VERSION,
            "previewStatus": "pending",
            "previewNeedsGeneration": True,
            "previewSourceFileName": Path(str(candidate.get("file_name") or "")).name,
            "previewSourceInstanceKey": str(candidate.get("instance_key") or ""),
        }
    )
    meta["localImport"] = local_import


def _candidate_path(model_root: Path, candidate: dict[str, Any]) -> Path | None:
    file_name = Path(str(candidate.get("file_name") or "")).name
    if not file_name:
        return None
    suffix = Path(file_name).suffix.lower()
    if suffix not in SUPPORTED_PREVIEW_SUFFIXES:
        return None
    target = (model_root / "instances" / file_name).resolve()
    try:
        target.relative_to(model_root.resolve())
    except ValueError:
        return None
    return target if target.is_file() else None


def _truncate_message(value: str, limit: int = 400) -> str:
    text = str(value or "").strip()
    return text[:limit] if text else ""


def _safe_render_env() -> dict[str, str]:
    allowed = {
        "PATH",
        "HOME",
        "TMPDIR",
        "TEMP",
        "TMP",
        "LANG",
        "LC_ALL",
        "NODE_OPTIONS",
        "MAKERHUB_CHROMIUM_PATH",
        "CHROMIUM_PATH",
        "PUPPETEER_EXECUTABLE_PATH",
    }
    return {key: value for key, value in os.environ.items() if key in allowed}


def _render_preview_png(model_file: Path, source_file_name: str) -> bytes:
    if not RENDERER_SCRIPT.is_file():
        raise RuntimeError("Three.js 预览渲染脚本不存在。")
    with tempfile.TemporaryDirectory(prefix="makerhub-preview-") as tmp:
        output_path = Path(tmp) / "preview.png"
        command = [
            "node",
            RENDERER_SCRIPT.as_posix(),
            "--input",
            model_file.as_posix(),
            "--output",
            output_path.as_posix(),
            "--name",
            source_file_name or model_file.name,
            "--size",
            str(DEFAULT_RENDER_SIZE),
        ]
        try:
            result = subprocess.run(
                command,
                cwd=ROOT_DIR.as_posix(),
                env=_safe_render_env(),
                capture_output=True,
                text=True,
                timeout=max(int(LOCAL_PREVIEW_TIMEOUT_SECONDS or 60), 10),
                check=False,
            )
        except FileNotFoundError as exc:
            raise RuntimeError("worker 容器缺少 Node.js，无法生成 Three.js 预览图。") from exc
        except subprocess.TimeoutExpired as exc:
            raise TimeoutError("Three.js 预览图生成超时。") from exc

        if result.returncode != 0:
            message = _truncate_message(result.stderr or result.stdout or "Three.js 预览图生成失败。")
            raise RuntimeError(message)
        try:
            data = output_path.read_bytes()
        except OSError as exc:
            raise RuntimeError("Three.js 没有输出有效预览图。") from exc
        if not data:
            raise RuntimeError("Three.js 输出的预览图为空。")
        return data


def find_pending_local_preview_model() -> tuple[Path, dict[str, Any], dict[str, Any]] | None:
    archive_root = ARCHIVE_DIR.resolve()
    if not archive_root.exists():
        return None
    for meta_path in sorted(archive_root.rglob("meta.json")):
        if not meta_path.is_file():
            continue
        model_root = meta_path.parent
        meta = _read_meta(meta_path)
        if not meta or str(meta.get("source") or "").strip().lower() != "local":
            continue
        local_import = meta.get("localImport") if isinstance(meta.get("localImport"), dict) else {}
        persisted_status = str(local_import.get("previewStatus") or "").strip().lower()
        explicitly_queued = bool(local_import.get("previewNeedsGeneration")) or persisted_status == "running"
        if not explicitly_queued:
            continue
        state = build_local_preview_state(meta, model_root)
        if state and _status(meta) == "running":
            candidate = state.get("candidate") if isinstance(state.get("candidate"), dict) else {}
            _mark_preview_pending(meta, candidate)
            _write_meta(model_root, meta, reason="local_preview_stale_running")
            continue
        if not state.get("needs_generation"):
            _write_meta(model_root, meta, reason="local_preview_not_needed")
            continue
        candidate = state.get("candidate") if isinstance(state.get("candidate"), dict) else first_previewable_instance(meta, model_root)
        if not candidate:
            continue
        return model_root, meta, candidate
    return None


def run_local_preview_generation_once() -> dict[str, Any]:
    pending = find_pending_local_preview_model()
    if pending is None:
        return {"processed": False, "reason": "idle"}

    model_root, meta, candidate = pending
    model_dir = _model_dir(model_root)
    source_file_name = Path(str(candidate.get("file_name") or "")).name
    source_instance_key = str(candidate.get("instance_key") or "")
    model_file = _candidate_path(model_root, candidate)
    if model_file is None:
        record_generated_preview_failure(
            meta,
            message="没有找到可用于生成封面的模型文件。",
            status="unsupported",
            source_file_name=source_file_name,
            source_instance_key=source_instance_key,
        )
        _write_meta(model_root, meta, reason="local_preview_unsupported")
        return {"processed": True, "status": "unsupported", "model_dir": model_dir}

    try:
        file_size = int(model_file.stat().st_size)
    except OSError:
        file_size = 0
    if file_size <= 0:
        record_generated_preview_failure(
            meta,
            message="模型文件为空，已跳过自动封面生成。",
            status="unsupported",
            source_file_name=source_file_name,
            source_instance_key=source_instance_key,
        )
        _write_meta(model_root, meta, reason="local_preview_empty_file")
        return {"processed": True, "status": "unsupported", "model_dir": model_dir}

    if file_size > int(LOCAL_PREVIEW_MAX_BYTES or 24 * 1024 * 1024):
        record_generated_preview_failure(
            meta,
            message=f"模型文件超过自动封面上限 {int(LOCAL_PREVIEW_MAX_BYTES or 0) // 1024 // 1024 or 24} MB，已跳过。",
            status="too_large",
            source_file_name=source_file_name,
            source_instance_key=source_instance_key,
        )
        _write_meta(model_root, meta, reason="local_preview_too_large")
        append_business_log(
            "model",
            "local_model_preview_skipped",
            "本地模型 Three.js 封面已跳过：文件过大。",
            model_dir=model_dir,
            source_file_name=source_file_name,
            file_size=file_size,
        )
        return {"processed": True, "status": "too_large", "model_dir": model_dir}

    local_import = meta.get("localImport") if isinstance(meta.get("localImport"), dict) else {}
    local_import.update(
        {
            "previewStatus": "running",
            "previewNeedsGeneration": True,
            "previewStartedAt": china_now_iso(),
            "previewSourceFileName": source_file_name,
            "previewSourceInstanceKey": source_instance_key,
        }
    )
    meta["localImport"] = local_import
    _write_meta(model_root, meta, reason="local_preview_started")
    append_business_log(
        "model",
        "local_model_preview_started",
        "本地模型 Three.js 封面开始生成。",
        model_dir=model_dir,
        source_file_name=source_file_name,
        file_size=file_size,
    )

    try:
        image_bytes = _render_preview_png(model_file, source_file_name)
        refreshed_meta = _read_meta(model_root / "meta.json") or meta
        apply_generated_preview_image(
            model_root=model_root,
            meta=refreshed_meta,
            image_bytes=image_bytes,
            mime_type="image/png",
            source_file_name=source_file_name,
            source_instance_key=source_instance_key,
        )
        _write_meta(model_root, refreshed_meta, reason="local_preview_generated")
        append_business_log(
            "model",
            "local_model_preview_generated",
            "本地模型 Three.js 封面已生成。",
            model_dir=model_dir,
            source_file_name=source_file_name,
            image_size=len(image_bytes),
        )
        return {"processed": True, "status": "success", "model_dir": model_dir}
    except TimeoutError as exc:
        failure_status = "failed"
        message = str(exc)
    except Exception as exc:
        failure_status = "failed"
        message = str(exc) or "Three.js 预览图生成失败。"

    refreshed_meta = _read_meta(model_root / "meta.json") or meta
    record_generated_preview_failure(
        refreshed_meta,
        message=message,
        status=failure_status,
        source_file_name=source_file_name,
        source_instance_key=source_instance_key,
    )
    _write_meta(model_root, refreshed_meta, reason="local_preview_failed")
    append_business_log(
        "model",
        "local_model_preview_failed",
        "本地模型 Three.js 封面生成失败。",
        level="warning",
        model_dir=model_dir,
        source_file_name=source_file_name,
        error=message,
    )
    return {"processed": True, "status": failure_status, "model_dir": model_dir}
