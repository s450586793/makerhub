import hashlib
import html
import json
import re
import shutil
import subprocess
import sys
import threading
import time
import xml.etree.ElementTree as ET
import zipfile
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from app.core.settings import ARCHIVE_DIR, LOGS_DIR
from app.core.store import JsonStore
from app.services.legacy_archiver import sanitize_filename
from app.services.task_state import TaskStateStore


ORGANIZER_LOG_PATH = LOGS_DIR / "organizer.log"
ORGANIZER_POLL_INTERVAL_SECONDS = 5
ORGANIZER_MIN_FILE_AGE_SECONDS = 2
ORGANIZER_TASK_LIMIT = 50
ORGANIZER_MAX_FILES_PER_CYCLE = 1
ORGANIZER_LIBRARY_INDEX_CACHE_TTL_SECONDS = 300
ORGANIZER_PREVIEW_LIMIT = 6
PREVIEW_IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp", ".bmp", ".gif"}
ORGANIZER_IGNORED_DIR_NAMES = {"_duplicates", "_failed", "_skipped"}


def _now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _append_organizer_log(event: str, **payload) -> None:
    try:
        ORGANIZER_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        with ORGANIZER_LOG_PATH.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps({"time": _now_iso(), "event": event, **payload}, ensure_ascii=False) + "\n")
    except OSError:
        return


def _task_id_from_fingerprint(fingerprint: str) -> str:
    return hashlib.sha1(fingerprint.encode("utf-8")).hexdigest()[:16]


def _safe_relative_string(path: Path, base: Path) -> str:
    try:
        return path.resolve().relative_to(base.resolve()).as_posix()
    except ValueError:
        return path.name


def _ensure_parent(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)


def _normalize_identity_text(value: Any) -> str:
    return " ".join(str(value or "").strip().lower().split())


def _normalize_loose_identity_text(value: Any) -> str:
    text = html.unescape(str(value or "")).strip().lower()
    if not text:
        return ""
    text = re.sub(r"[\s\-_:/|\\.,，。;；'\"`~!！?？()\[\]{}<>《》【】（）、+]+", "", text)
    return text


def _unique_non_empty(values: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        text = str(value or "").strip()
        if not text or text in seen:
            continue
        seen.add(text)
        result.append(text)
    return result


def _coerce_dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _safe_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


class LocalOrganizerService:
    def __init__(
        self,
        *,
        store: Optional[JsonStore] = None,
        task_store: Optional[TaskStateStore] = None,
    ) -> None:
        self.store = store or JsonStore()
        self.task_store = task_store or TaskStateStore()
        self._thread: Optional[threading.Thread] = None
        self._worker_process: Optional[subprocess.Popen[str]] = None
        self._worker_source_path = ""
        self._start_lock = threading.Lock()
        self._library_index_cache: Optional[dict[str, dict[str, dict[str, Any]]]] = None
        self._library_index_cache_root = ""
        self._library_index_cache_at = 0.0

    def start(self) -> None:
        with self._start_lock:
            if self._thread and self._thread.is_alive():
                return
            self._thread = threading.Thread(target=self._run_loop, name="makerhub-local-organizer", daemon=True)
            self._thread.start()

    def _run_loop(self) -> None:
        while True:
            try:
                self.run_once()
            except Exception as exc:
                _append_organizer_log("loop_error", error=str(exc))
            time.sleep(ORGANIZER_POLL_INTERVAL_SECONDS)

    def run_once(self) -> None:
        self._poll_worker()
        if self._worker_process and self._worker_process.poll() is None:
            return

        config = self.store.load()
        organizer = config.organizer

        source_raw = str(organizer.source_dir or "").strip()
        target_raw = str(organizer.target_dir or "").strip()
        if not source_raw:
            return
        if not target_raw:
            return
        source_dir = Path(source_raw).expanduser()
        target_dir = Path(target_raw).expanduser()

        source_dir.mkdir(parents=True, exist_ok=True)
        library_root = self._resolve_library_root(target_dir)
        library_root.mkdir(parents=True, exist_ok=True)

        try:
            source_resolved = source_dir.resolve()
            library_resolved = library_root.resolve()
            if (
                source_resolved == library_resolved
                or library_resolved.is_relative_to(source_resolved)
                or source_resolved.is_relative_to(library_resolved)
            ):
                _append_organizer_log(
                    "invalid_config",
                    source_dir=str(source_dir),
                    target_dir=str(library_root),
                    reason="target_inside_source",
                )
                return
        except OSError:
            pass

        candidates = self._iter_candidates(source_dir)
        if not candidates:
            return

        pending_count = len(candidates)
        if pending_count > ORGANIZER_MAX_FILES_PER_CYCLE:
            _append_organizer_log(
                "backlog_limited",
                source_dir=source_dir.as_posix(),
                pending_count=pending_count,
                processing_limit=ORGANIZER_MAX_FILES_PER_CYCLE,
            )

        candidate = candidates[0]
        self._spawn_worker(
            source_path=candidate,
            source_dir=source_dir,
            library_root=library_root,
            move_files=bool(organizer.move_files),
        )

    def process_candidate(
        self,
        *,
        source_path: Path,
        source_dir: Path,
        library_root: Path,
        move_files: bool,
    ) -> None:
        if not source_path.exists() or not source_path.is_file():
            return

        library_index = self._get_library_index(library_root)
        existing_items = self.task_store.load_organize_tasks().get("items") or []
        known_by_fingerprint = {
            str(item.get("fingerprint") or ""): item
            for item in existing_items
            if str(item.get("fingerprint") or "")
        }
        fingerprint = self._fingerprint(source_path)
        if not fingerprint:
            return

        existing = known_by_fingerprint.get(fingerprint) or {}
        if str(existing.get("status") or "").lower() in {"running", "success", "skipped"}:
            return

        analysis = self._inspect_3mf(source_path)
        duplicate_match = library_index["configs"].get(str(analysis.get("config_fingerprint") or "")) if analysis else None
        model_match = self._match_existing_model(library_index["models"], analysis) if analysis else None

        if duplicate_match:
            self._handle_duplicate_file(
                source_path=source_path,
                source_dir=source_dir,
                move_files=move_files,
                fingerprint=fingerprint,
                duplicate_match=duplicate_match,
            )
            return

        result = self._organize_file(
            source_path=source_path,
            source_dir=source_dir,
            library_root=library_root,
            move_files=move_files,
            fingerprint=fingerprint,
            existing=existing,
            analysis=analysis,
            matched_model=model_match,
        )
        if result:
            self._register_library_item(library_index, result)

    def _poll_worker(self) -> None:
        process = self._worker_process
        if process is None:
            return
        return_code = process.poll()
        if return_code is None:
            return
        _append_organizer_log(
            "worker_exited",
            source=self._worker_source_path,
            return_code=return_code,
        )
        self._worker_process = None
        self._worker_source_path = ""

    def _spawn_worker(
        self,
        *,
        source_path: Path,
        source_dir: Path,
        library_root: Path,
        move_files: bool,
    ) -> None:
        command = [
            sys.executable,
            "-m",
            "app.services.local_organizer_worker",
            "--source-path",
            source_path.as_posix(),
            "--source-dir",
            source_dir.as_posix(),
            "--library-root",
            library_root.as_posix(),
        ]
        if move_files:
            command.append("--move-files")

        self._worker_process = subprocess.Popen(
            command,
            cwd=Path(__file__).resolve().parents[2],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            text=True,
        )
        self._worker_source_path = source_path.as_posix()
        _append_organizer_log(
            "worker_started",
            source=self._worker_source_path,
            pid=self._worker_process.pid,
        )

    def _iter_candidates(self, source_dir: Path) -> list[Path]:
        now = time.time()
        candidates: list[Path] = []
        for path in source_dir.rglob("*"):
            if not path.is_file() or path.suffix.lower() != ".3mf":
                continue
            if self._is_managed_output(path, source_dir):
                continue
            try:
                stat = path.stat()
            except OSError:
                continue
            if now - stat.st_mtime < ORGANIZER_MIN_FILE_AGE_SECONDS:
                continue
            candidates.append(path)
        candidates.sort(key=lambda item: item.as_posix().lower())
        return candidates

    def _is_managed_output(self, path: Path, source_dir: Path) -> bool:
        try:
            relative = path.relative_to(source_dir)
        except ValueError:
            return False

        if any(part in ORGANIZER_IGNORED_DIR_NAMES for part in relative.parts[:-1]):
            return True

        # Organizer-generated 3MF files always live under `<model_dir>/instances/`.
        # When `/app/local` is mounted to the same host path as `/app/archive/local`,
        # we must skip those files or the organizer will recursively re-import itself.
        if "instances" in relative.parts[:-1]:
            return True

        current = path.parent
        while current != source_dir and current != current.parent:
            if (current / "meta.json").exists():
                return True
            current = current.parent

        return False

    def _resolve_library_root(self, target_dir: Path) -> Path:
        return target_dir.expanduser()

    def _fingerprint(self, path: Path) -> str:
        try:
            stat = path.stat()
        except OSError:
            return ""
        return f"{path.resolve()}::{stat.st_size}::{stat.st_mtime_ns}"

    def _sha256_file(self, path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                if chunk:
                    digest.update(chunk)
        return digest.hexdigest()

    def _parse_3mf_metadata(self, source_path: Path) -> dict[str, str]:
        metadata: dict[str, str] = {}
        try:
            with zipfile.ZipFile(source_path) as archive:
                model_member = ""
                for name in archive.namelist():
                    if str(name or "").lower() == "3d/3dmodel.model":
                        model_member = name
                        break
                if not model_member:
                    return metadata

                root = ET.fromstring(archive.read(model_member))
                for node in root.findall(".//{*}metadata"):
                    key = str(node.attrib.get("name") or "").strip()
                    if not key:
                        continue
                    value = node.text or node.attrib.get("value") or ""
                    metadata[key] = html.unescape(str(value or "")).strip()
        except (OSError, zipfile.BadZipFile, zipfile.LargeZipFile, ET.ParseError, KeyError):
            return {}
        return metadata

    def _derive_model_key(self, analysis: dict[str, Any]) -> str:
        design_model_id = str(analysis.get("design_model_id") or "").strip()
        if design_model_id:
            return f"design_model:{design_model_id}"

        title = _normalize_identity_text(analysis.get("model_title") or analysis.get("profile_title") or analysis.get("source_title"))
        designer = _normalize_identity_text(analysis.get("designer"))
        if title:
            return f"title_designer:{title}|{designer}"
        return ""

    def _build_model_match_keys(
        self,
        *,
        design_model_id: Any,
        title_candidates: list[Any],
        designer_candidates: list[Any],
    ) -> list[str]:
        keys: list[str] = []
        design_model_text = str(design_model_id or "").strip()
        if design_model_text:
            keys.append(f"design_model:{design_model_text}")

        normalized_titles = _unique_non_empty([_normalize_identity_text(item) for item in title_candidates])
        normalized_designers = _unique_non_empty([_normalize_identity_text(item) for item in designer_candidates])
        loose_titles = _unique_non_empty([_normalize_loose_identity_text(item) for item in title_candidates])
        loose_designers = _unique_non_empty([_normalize_loose_identity_text(item) for item in designer_candidates])

        for title in normalized_titles:
            for designer in normalized_designers:
                keys.append(f"title_designer:{title}|{designer}")
        for title in loose_titles:
            for designer in loose_designers:
                keys.append(f"title_designer_loose:{title}|{designer}")

        # Title-only aliases are only used as a unique fallback when IDs or author
        # metadata are missing/inconsistent between the local 3MF and the archived model.
        for title in normalized_titles:
            if len(title) >= 4:
                keys.append(f"title_only:{title}")
        for title in loose_titles:
            if len(title) >= 4:
                keys.append(f"title_only_loose:{title}")

        return _unique_non_empty(keys)

    def _model_match_keys_from_analysis(self, analysis: dict[str, Any]) -> list[str]:
        metadata = _coerce_dict(analysis.get("metadata"))
        return self._build_model_match_keys(
            design_model_id=analysis.get("design_model_id"),
            title_candidates=[
                analysis.get("model_title"),
                analysis.get("profile_title"),
                analysis.get("source_title"),
            ],
            designer_candidates=[
                analysis.get("designer"),
                metadata.get("Designer"),
                metadata.get("ProfileUserName"),
            ],
        )

    def _derive_config_fingerprint(self, analysis: dict[str, Any]) -> str:
        design_profile_id = str(analysis.get("design_profile_id") or "").strip()
        if design_profile_id:
            return f"design_profile:{design_profile_id}"
        file_hash = str(analysis.get("file_hash") or "").strip()
        if file_hash:
            return f"sha256:{file_hash}"
        return ""

    def _inspect_3mf(self, source_path: Path) -> dict[str, Any]:
        file_hash = self._sha256_file(source_path)
        metadata = self._parse_3mf_metadata(source_path)
        model_title = str(metadata.get("Title") or source_path.stem).strip() or source_path.stem
        profile_title = str(metadata.get("ProfileTitle") or model_title).strip() or model_title
        designer = str(metadata.get("Designer") or metadata.get("ProfileUserName") or "").strip()
        analysis = {
            "source_title": source_path.stem.strip() or source_path.name,
            "model_title": model_title,
            "profile_title": profile_title,
            "designer": designer,
            "design_model_id": str(metadata.get("DesignModelId") or "").strip(),
            "design_profile_id": str(metadata.get("DesignProfileId") or "").strip(),
            "file_hash": file_hash,
            "metadata": metadata,
        }
        analysis["model_key"] = self._derive_model_key(analysis)
        analysis["config_fingerprint"] = self._derive_config_fingerprint(analysis)
        return analysis

    def _build_library_index(self, library_root: Path) -> dict[str, dict[str, dict[str, Any]]]:
        models: dict[str, dict[str, Any]] = {}
        configs: dict[str, dict[str, Any]] = {}
        ambiguous_model_keys: set[str] = set()

        for meta_path in sorted(library_root.rglob("meta.json")):
            try:
                payload = json.loads(meta_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if not isinstance(payload, dict):
                continue

            model_root = meta_path.parent
            model_info = {
                "model_root": model_root,
                "model_dir": self._model_dir_string(model_root),
                "title": str(payload.get("title") or model_root.name),
                "author": self._author_name(payload),
            }

            for model_key in self._model_match_keys_from_meta(payload):
                existing_info = models.get(model_key)
                if existing_info and existing_info.get("model_dir") != model_info["model_dir"]:
                    ambiguous_model_keys.add(model_key)
                    continue
                models[model_key] = model_info

            for config_key, config_info in self._config_entries_from_meta(payload, model_root):
                configs.setdefault(config_key, config_info)

        for model_key in ambiguous_model_keys:
            models.pop(model_key, None)

        return {"models": models, "configs": configs}

    def _get_library_index(self, library_root: Path, *, force: bool = False) -> dict[str, dict[str, dict[str, Any]]]:
        root_key = library_root.resolve().as_posix()
        now = time.time()
        if (
            not force
            and self._library_index_cache is not None
            and self._library_index_cache_root == root_key
            and now - self._library_index_cache_at < ORGANIZER_LIBRARY_INDEX_CACHE_TTL_SECONDS
        ):
            return self._library_index_cache

        library_index = self._build_library_index(library_root)
        self._library_index_cache = library_index
        self._library_index_cache_root = root_key
        self._library_index_cache_at = now
        _append_organizer_log(
            "library_index_rebuilt",
            root=root_key,
            model_count=len(library_index.get("models") or {}),
            config_count=len(library_index.get("configs") or {}),
        )
        return library_index

    def _author_name(self, meta: dict[str, Any]) -> str:
        author = meta.get("author")
        if isinstance(author, dict):
            return str(author.get("name") or "").strip()
        if isinstance(author, str):
            return author.strip()
        return ""

    def _model_key_from_meta(self, meta: dict[str, Any]) -> str:
        local_import = _coerce_dict(meta.get("localImport"))
        design_model_id = str(local_import.get("designModelId") or meta.get("id") or "").strip()
        if design_model_id:
            return f"design_model:{design_model_id}"

        title = _normalize_identity_text(meta.get("title"))
        author = _normalize_identity_text(self._author_name(meta))
        if title:
            return f"title_designer:{title}|{author}"
        return ""

    def _model_match_keys_from_meta(self, meta: dict[str, Any]) -> list[str]:
        local_import = _coerce_dict(meta.get("localImport"))
        return self._build_model_match_keys(
            design_model_id=local_import.get("designModelId") or meta.get("id"),
            title_candidates=[
                meta.get("title"),
                meta.get("baseName"),
            ],
            designer_candidates=[
                self._author_name(meta),
            ],
        )

    def _match_existing_model(
        self,
        models_index: dict[str, dict[str, Any]],
        analysis: dict[str, Any],
    ) -> Optional[dict[str, Any]]:
        for model_key in self._model_match_keys_from_analysis(analysis):
            matched = models_index.get(model_key)
            if matched:
                return {**matched, "match_key": model_key}
        return None

    def _config_entries_from_meta(self, meta: dict[str, Any], model_root: Path) -> list[tuple[str, dict[str, Any]]]:
        entries: list[tuple[str, dict[str, Any]]] = []
        instances = meta.get("instances") if isinstance(meta.get("instances"), list) else []
        for inst in instances:
            if not isinstance(inst, dict):
                continue
            profile_id = str(inst.get("profileId") or inst.get("profile_id") or inst.get("profileID") or "").strip()
            local_import = _coerce_dict(inst.get("localImport"))
            file_hash = str(local_import.get("fileHash") or "").strip()
            config_fingerprint = str(local_import.get("configFingerprint") or "").strip()

            key = ""
            if profile_id:
                key = f"design_profile:{profile_id}"
            elif config_fingerprint:
                key = config_fingerprint
            elif file_hash:
                key = f"sha256:{file_hash}"

            if not key:
                continue

            entries.append(
                (
                    key,
                    {
                        "model_root": model_root,
                        "model_dir": self._model_dir_string(model_root),
                        "model_title": str(meta.get("title") or model_root.name),
                        "instance_title": str(inst.get("title") or inst.get("name") or inst.get("fileName") or ""),
                        "instance_id": str(inst.get("id") or ""),
                        "profile_id": profile_id,
                    },
                )
            )

        local_import = _coerce_dict(meta.get("localImport"))
        top_key = str(local_import.get("configFingerprint") or "").strip()
        if not top_key:
            top_hash = str(local_import.get("fileHash") or "").strip()
            if top_hash:
                top_key = f"sha256:{top_hash}"
        if top_key:
            entries.append(
                (
                    top_key,
                    {
                        "model_root": model_root,
                        "model_dir": self._model_dir_string(model_root),
                        "model_title": str(meta.get("title") or model_root.name),
                        "instance_title": "",
                        "instance_id": "",
                        "profile_id": str(local_import.get("designProfileId") or "").strip(),
                    },
                )
            )

        return entries

    def _handle_duplicate_file(
        self,
        *,
        source_path: Path,
        source_dir: Path,
        move_files: bool,
        fingerprint: str,
        duplicate_match: dict[str, Any],
    ) -> None:
        task_id = _task_id_from_fingerprint(fingerprint)
        source_path_text = source_path.as_posix()
        target_path = ""
        status_message = f"该 3MF 与模型库现有配置重复，已跳过。命中模型：{duplicate_match.get('model_title') or duplicate_match.get('model_dir') or '未知模型'}"

        if duplicate_match.get("instance_title"):
            status_message += f" / 配置：{duplicate_match.get('instance_title')}"

        try:
            if move_files:
                duplicates_dir = source_dir / "_duplicates"
                duplicates_dir.mkdir(parents=True, exist_ok=True)
                target = self._ensure_unique_filename(duplicates_dir, source_path.name)
                shutil.move(str(source_path), str(target))
                target_path = target.as_posix()

            self.task_store.upsert_organize_task(
                {
                    "id": task_id,
                    "title": source_path.stem.strip() or source_path.name,
                    "file_name": source_path.name,
                    "source_dir": source_dir.as_posix(),
                    "target_dir": str(duplicate_match.get("model_dir") or ""),
                    "source_path": source_path_text,
                    "target_path": target_path,
                    "status": "skipped",
                    "message": status_message,
                    "progress": 100,
                    "updated_at": _now_iso(),
                    "move_files": move_files,
                    "fingerprint": fingerprint,
                    "model_dir": str(duplicate_match.get("model_dir") or ""),
                },
                limit=ORGANIZER_TASK_LIMIT,
            )
            _append_organizer_log(
                "duplicate_skipped",
                source=source_path_text,
                duplicate_model=str(duplicate_match.get("model_dir") or ""),
                duplicate_instance=str(duplicate_match.get("instance_id") or ""),
                moved_to=target_path,
            )
        except Exception as exc:
            self.task_store.upsert_organize_task(
                {
                    "id": task_id,
                    "title": source_path.stem.strip() or source_path.name,
                    "file_name": source_path.name,
                    "source_dir": source_dir.as_posix(),
                    "target_dir": str(duplicate_match.get("model_dir") or ""),
                    "source_path": source_path_text,
                    "target_path": target_path,
                    "status": "failed",
                    "message": f"重复文件处理失败：{exc}",
                    "progress": 0,
                    "updated_at": _now_iso(),
                    "move_files": move_files,
                    "fingerprint": fingerprint,
                    "model_dir": str(duplicate_match.get("model_dir") or ""),
                },
                limit=ORGANIZER_TASK_LIMIT,
            )
            _append_organizer_log("duplicate_skip_failed", source=source_path_text, error=str(exc))

    def _organize_file(
        self,
        *,
        source_path: Path,
        source_dir: Path,
        library_root: Path,
        move_files: bool,
        fingerprint: str,
        existing: dict,
        analysis: Optional[dict[str, Any]] = None,
        matched_model: Optional[dict[str, Any]] = None,
    ) -> Optional[dict[str, Any]]:
        task_id = _task_id_from_fingerprint(fingerprint)
        source_path_text = source_path.as_posix()
        relative_source = _safe_relative_string(source_path, source_dir)
        source_title = source_path.stem.strip() or source_path.name
        now_iso = _now_iso()

        self.task_store.upsert_organize_task(
            {
                "id": task_id,
                "title": source_title,
                "file_name": source_path.name,
                "source_dir": source_dir.as_posix(),
                "target_dir": library_root.as_posix(),
                "source_path": source_path_text,
                "status": "running",
                "message": "正在整理本地 3MF 文件。",
                "progress": 15,
                "updated_at": now_iso,
                "move_files": move_files,
                "fingerprint": fingerprint,
            },
            limit=ORGANIZER_TASK_LIMIT,
        )

        model_root: Optional[Path] = None
        target_file: Optional[Path] = None

        try:
            model_root = self._prepare_model_root(
                library_root,
                source_path,
                existing=existing,
                matched_model=matched_model,
            )
            instances_dir = model_root / "instances"
            images_dir = model_root / "images"
            instances_dir.mkdir(parents=True, exist_ok=True)
            images_dir.mkdir(parents=True, exist_ok=True)

            target_file = self._copy_or_move_file(source_path, instances_dir, move_files=move_files)

            progress_message = "3MF 已写入模型目录，正在生成元数据。"
            if matched_model:
                progress_message = "已命中现有模型目录，正在补充新的打印配置。"

            self.task_store.upsert_organize_task(
                {
                    "id": task_id,
                    "title": source_title,
                    "file_name": source_path.name,
                    "source_dir": source_dir.as_posix(),
                    "target_dir": library_root.as_posix(),
                    "source_path": source_path_text,
                    "target_path": target_file.as_posix(),
                    "status": "running",
                    "message": progress_message,
                    "progress": 70,
                    "updated_at": _now_iso(),
                    "move_files": move_files,
                    "fingerprint": fingerprint,
                    "model_dir": self._model_dir_string(model_root),
                },
                limit=ORGANIZER_TASK_LIMIT,
            )

            preview_paths = self._extract_preview_images(target_file, images_dir)
            if matched_model:
                meta = self._append_instance_to_existing_meta(
                    model_root=model_root,
                    target_file=target_file,
                    source_relative_path=relative_source,
                    original_filename=source_path.name,
                    move_files=move_files,
                    fingerprint=fingerprint,
                    preview_paths=preview_paths,
                    analysis=analysis or {},
                )
                success_message = "本地 3MF 已并入现有模型目录。"
            else:
                meta = self._build_meta(
                    model_root=model_root,
                    title=source_title,
                    source_relative_path=relative_source,
                    original_filename=source_path.name,
                    target_file=target_file,
                    move_files=move_files,
                    fingerprint=fingerprint,
                    preview_paths=preview_paths,
                    analysis=analysis or {},
                )
                success_message = "本地 3MF 已整理完成。"

            (model_root / "meta.json").write_text(
                json.dumps(meta, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )

            self.task_store.upsert_organize_task(
                {
                    "id": task_id,
                    "title": source_title,
                    "file_name": source_path.name,
                    "source_dir": source_dir.as_posix(),
                    "target_dir": library_root.as_posix(),
                    "source_path": source_path_text,
                    "target_path": target_file.as_posix(),
                    "status": "success",
                    "message": success_message,
                    "progress": 100,
                    "updated_at": _now_iso(),
                    "move_files": move_files,
                    "fingerprint": fingerprint,
                    "model_dir": self._model_dir_string(model_root),
                },
                limit=ORGANIZER_TASK_LIMIT,
            )
            _append_organizer_log(
                "organized",
                source=source_path_text,
                target=str(target_file),
                model_dir=self._model_dir_string(model_root),
                move_files=move_files,
                reused_model=bool(matched_model),
            )
            return {
                "model_root": model_root,
                "meta": meta,
                "analysis": analysis or {},
            }
        except Exception as exc:
            self.task_store.upsert_organize_task(
                {
                    "id": task_id,
                    "title": source_title,
                    "file_name": source_path.name,
                    "source_dir": source_dir.as_posix(),
                    "target_dir": library_root.as_posix(),
                    "source_path": source_path_text,
                    "target_path": target_file.as_posix() if target_file else "",
                    "status": "failed",
                    "message": str(exc),
                    "progress": 0,
                    "updated_at": _now_iso(),
                    "move_files": move_files,
                    "fingerprint": fingerprint,
                    "model_dir": self._model_dir_string(model_root) if model_root else "",
                },
                limit=ORGANIZER_TASK_LIMIT,
            )
            _append_organizer_log("organize_failed", source=source_path_text, error=str(exc))
            return None

    def _prepare_model_root(
        self,
        library_root: Path,
        source_path: Path,
        *,
        existing: dict,
        matched_model: Optional[dict[str, Any]],
    ) -> Path:
        if matched_model:
            matched_root = matched_model.get("model_root")
            if isinstance(matched_root, Path) and matched_root.is_dir():
                matched_root.mkdir(parents=True, exist_ok=True)
                return matched_root

        existing_model_dir = str(existing.get("model_dir") or "").strip().strip("/")
        if existing_model_dir:
            try:
                existing_path = (ARCHIVE_DIR / existing_model_dir).resolve()
                if existing_path.is_dir():
                    existing_path.mkdir(parents=True, exist_ok=True)
                    return existing_path
            except OSError:
                pass

        stem = sanitize_filename(source_path.stem) or "local_model"
        base_name = f"LOCAL_{stem}"
        for index in range(0, 1000):
            candidate_name = base_name if index == 0 else f"{base_name}_{index + 1}"
            candidate = library_root / candidate_name
            if not candidate.exists():
                candidate.mkdir(parents=True, exist_ok=True)
                return candidate
        raise RuntimeError("无法为本地模型分配新的目标目录。")

    def _copy_or_move_file(self, source_path: Path, instances_dir: Path, *, move_files: bool) -> Path:
        target = self._ensure_unique_filename(instances_dir, source_path.name)
        _ensure_parent(target)
        if move_files:
            shutil.move(str(source_path), str(target))
        else:
            shutil.copy2(source_path, target)
        return target

    def _ensure_unique_filename(self, parent: Path, raw_name: str) -> Path:
        file_name = sanitize_filename(raw_name) or "model.3mf"
        suffix = Path(file_name).suffix or ".3mf"
        stem = Path(file_name).stem or "model"
        target = parent / f"{stem}{suffix}"
        index = 2
        while target.exists():
            target = parent / f"{stem}_{index}{suffix}"
            index += 1
        return target

    def _extract_preview_images(self, target_file: Path, images_dir: Path) -> list[str]:
        preview_paths: list[str] = []
        written_names: set[str] = set()

        try:
            with zipfile.ZipFile(target_file) as archive:
                image_members = [
                    info
                    for info in archive.infolist()
                    if not info.is_dir()
                    and Path(info.filename).suffix.lower() in PREVIEW_IMAGE_SUFFIXES
                    and info.file_size > 0
                ]
                image_members.sort(key=self._preview_priority)

                for info in image_members[:ORGANIZER_PREVIEW_LIMIT]:
                    original_name = Path(info.filename).name
                    suffix = Path(original_name).suffix.lower() or ".png"
                    stem = sanitize_filename(Path(original_name).stem) or "preview"
                    candidate_name = f"{stem}{suffix}"
                    index = 2
                    while candidate_name in written_names or (images_dir / candidate_name).exists():
                        candidate_name = f"{stem}_{index}{suffix}"
                        index += 1

                    data = archive.read(info)
                    if not data:
                        continue

                    target_image = images_dir / candidate_name
                    target_image.write_bytes(data)
                    written_names.add(candidate_name)
                    preview_paths.append(f"images/{candidate_name}")
        except (OSError, zipfile.BadZipFile, zipfile.LargeZipFile):
            return []

        return preview_paths

    def _preview_priority(self, info: zipfile.ZipInfo) -> tuple[int, str]:
        name = info.filename.lower()
        score = 100
        if "thumbnail" in name:
            score = 0
        elif "cover" in name or "preview" in name:
            score = 1
        elif "plate" in name:
            score = 2
        elif "metadata" in name:
            score = 3
        return (score, name)

    def _next_local_instance_id(self, instances: list[dict[str, Any]]) -> str:
        max_value = 0
        for item in instances:
            raw = str(item.get("id") or "")
            if raw.isdigit():
                max_value = max(max_value, int(raw))
                continue
            if raw.startswith("local-"):
                suffix = raw.split("local-", 1)[-1]
                if suffix.isdigit():
                    max_value = max(max_value, int(suffix))
        return f"local-{max_value + 1}"

    def _append_instance_to_existing_meta(
        self,
        *,
        model_root: Path,
        target_file: Path,
        source_relative_path: str,
        original_filename: str,
        move_files: bool,
        fingerprint: str,
        preview_paths: list[str],
        analysis: dict[str, Any],
    ) -> dict[str, Any]:
        meta_path = model_root / "meta.json"
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            meta = {}
        if not isinstance(meta, dict):
            meta = {}

        instances = meta.get("instances")
        if not isinstance(instances, list):
            instances = []
            meta["instances"] = instances

        now_iso = _now_iso()
        try:
            publish_iso = datetime.fromtimestamp(target_file.stat().st_mtime).isoformat(timespec="seconds")
        except OSError:
            publish_iso = now_iso

        cover_path = preview_paths[0] if preview_paths else ""
        gallery_items = [{"relPath": item} for item in preview_paths]
        instance_title = str(
            analysis.get("profile_title")
            or analysis.get("model_title")
            or analysis.get("source_title")
            or target_file.stem
        ).strip() or target_file.stem

        instances.append(
            {
                "id": self._next_local_instance_id(instances),
                "profileId": str(analysis.get("design_profile_id") or "").strip(),
                "title": instance_title,
                "titleTranslated": "",
                "publishTime": publish_iso,
                "downloadCount": 0,
                "printCount": 0,
                "prediction": 0,
                "weight": 0,
                "materialCnt": 0,
                "materialColorCnt": 0,
                "needAms": False,
                "plates": [],
                "pictures": gallery_items,
                "instanceFilaments": [],
                "summary": "本地补充导入的 3MF 配置。",
                "summaryTranslated": "",
                "name": target_file.name,
                "fileName": target_file.name,
                "sourceFileName": original_filename,
                "downloadUrl": "",
                "apiUrl": "",
                "thumbnailLocal": cover_path,
                "localImport": {
                    "sourcePath": source_relative_path,
                    "originalFilename": original_filename,
                    "organizedAt": now_iso,
                    "moveFiles": move_files,
                    "fingerprint": fingerprint,
                    "designProfileId": str(analysis.get("design_profile_id") or "").strip(),
                    "configFingerprint": str(analysis.get("config_fingerprint") or "").strip(),
                    "fileHash": str(analysis.get("file_hash") or "").strip(),
                },
            }
        )

        meta["update_time"] = now_iso
        return meta

    def _build_meta(
        self,
        *,
        model_root: Path,
        title: str,
        source_relative_path: str,
        original_filename: str,
        target_file: Path,
        move_files: bool,
        fingerprint: str,
        preview_paths: list[str],
        analysis: dict[str, Any],
    ) -> dict:
        now_iso = _now_iso()
        try:
            publish_iso = datetime.fromtimestamp(target_file.stat().st_mtime).isoformat(timespec="seconds")
        except OSError:
            publish_iso = now_iso

        cover_path = preview_paths[0] if preview_paths else ""
        gallery_items = [{"relPath": item} for item in preview_paths]
        author_name = str(analysis.get("designer") or "").strip() or "本地整理"
        model_title = str(analysis.get("model_title") or title).strip() or title
        profile_title = str(analysis.get("profile_title") or model_title).strip() or model_title
        design_model_id = str(analysis.get("design_model_id") or "").strip()
        design_profile_id = str(analysis.get("design_profile_id") or "").strip()
        config_fingerprint = str(analysis.get("config_fingerprint") or "").strip()
        file_hash = str(analysis.get("file_hash") or "").strip()

        return {
            "id": design_model_id,
            "title": model_title,
            "source": "local",
            "url": "",
            "baseName": model_root.name,
            "collectDate": now_iso,
            "publishedAt": publish_iso,
            "update_time": now_iso,
            "cover": cover_path,
            "designImages": gallery_items,
            "summaryImages": [],
            "summary": {
                "text": "从本地目录整理导入的 3MF 文件。",
                "html": "<p>从本地目录整理导入的 3MF 文件。</p>",
            },
            "author": {
                "name": author_name,
                "url": "",
            },
            "tags": ["本地导入"],
            "tagsOriginal": [],
            "stats": {
                "likes": 0,
                "favorites": 0,
                "downloads": 0,
                "prints": 0,
                "comments": 0,
                "views": 0,
            },
            "comments": [],
            "attachments": [],
            "instances": [
                {
                    "id": "local-default",
                    "profileId": design_profile_id,
                    "title": profile_title,
                    "name": target_file.name,
                    "machine": "本地 3MF",
                    "publishedAt": publish_iso,
                    "publishTime": publish_iso,
                    "summary": "该文件来自本地整理目录，可直接下载 3MF。",
                    "thumbnailLocal": cover_path,
                    "pictures": gallery_items,
                    "fileName": target_file.name,
                    "sourceFileName": original_filename,
                    "downloadCount": 0,
                    "printCount": 0,
                    "plateCount": 0,
                    "localImport": {
                        "sourcePath": source_relative_path,
                        "originalFilename": original_filename,
                        "organizedAt": now_iso,
                        "moveFiles": move_files,
                        "fingerprint": fingerprint,
                        "designProfileId": design_profile_id,
                        "configFingerprint": config_fingerprint,
                        "fileHash": file_hash,
                    },
                }
            ],
            "localImport": {
                "sourcePath": source_relative_path,
                "originalFilename": original_filename,
                "organizedAt": now_iso,
                "moveFiles": move_files,
                "fingerprint": fingerprint,
                "designModelId": design_model_id,
                "designProfileId": design_profile_id,
                "modelKey": str(analysis.get("model_key") or "").strip(),
                "configFingerprint": config_fingerprint,
                "fileHash": file_hash,
            },
        }

    def _register_library_item(self, library_index: dict[str, dict[str, dict[str, Any]]], result: dict[str, Any]) -> None:
        model_root = result.get("model_root")
        meta = result.get("meta")
        if not isinstance(model_root, Path) or not isinstance(meta, dict):
            return

        model_info = {
            "model_root": model_root,
            "model_dir": self._model_dir_string(model_root),
            "title": str(meta.get("title") or model_root.name),
            "author": self._author_name(meta),
        }
        for model_key in self._model_match_keys_from_meta(meta):
            if not model_key:
                continue
            existing_info = library_index.setdefault("models", {}).get(model_key)
            if existing_info and existing_info.get("model_dir") != model_info["model_dir"]:
                library_index["models"].pop(model_key, None)
                continue
            library_index["models"][model_key] = model_info

        for config_key, config_info in self._config_entries_from_meta(meta, model_root):
            if config_key:
                library_index.setdefault("configs", {})[config_key] = config_info

    def _model_dir_string(self, model_root: Optional[Path]) -> str:
        if not model_root:
            return ""
        try:
            return model_root.resolve().relative_to(ARCHIVE_DIR.resolve()).as_posix()
        except ValueError:
            return model_root.name
