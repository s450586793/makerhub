import hashlib
import json
import shutil
import threading
import time
import zipfile
from datetime import datetime
from pathlib import Path
from typing import Optional

from app.core.settings import ARCHIVE_DIR, LOGS_DIR
from app.core.store import JsonStore
from app.services.legacy_archiver import sanitize_filename
from app.services.task_state import TaskStateStore


ORGANIZER_LOG_PATH = LOGS_DIR / "organizer.log"
ORGANIZER_POLL_INTERVAL_SECONDS = 5
ORGANIZER_MIN_FILE_AGE_SECONDS = 2
ORGANIZER_TASK_LIMIT = 50
ORGANIZER_PREVIEW_LIMIT = 6
PREVIEW_IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp", ".bmp", ".gif"}


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
        self._start_lock = threading.Lock()

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

        existing_items = self.task_store.load_organize_tasks().get("items") or []
        known_by_fingerprint = {
            str(item.get("fingerprint") or ""): item
            for item in existing_items
            if str(item.get("fingerprint") or "")
        }

        for candidate in self._iter_candidates(source_dir):
            fingerprint = self._fingerprint(candidate)
            if not fingerprint:
                continue

            existing = known_by_fingerprint.get(fingerprint) or {}
            if str(existing.get("status") or "").lower() in {"running", "success", "skipped"}:
                continue

            self._organize_file(
                source_path=candidate,
                source_dir=source_dir,
                library_root=library_root,
                move_files=bool(organizer.move_files),
                fingerprint=fingerprint,
                existing=existing,
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

    def _organize_file(
        self,
        *,
        source_path: Path,
        source_dir: Path,
        library_root: Path,
        move_files: bool,
        fingerprint: str,
        existing: dict,
    ) -> None:
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
            model_root = self._prepare_model_root(library_root, source_path, existing=existing)
            instances_dir = model_root / "instances"
            images_dir = model_root / "images"
            instances_dir.mkdir(parents=True, exist_ok=True)
            images_dir.mkdir(parents=True, exist_ok=True)

            target_file = self._copy_or_move_file(source_path, instances_dir, move_files=move_files)

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
                    "message": "3MF 已写入模型目录，正在生成元数据。",
                    "progress": 70,
                    "updated_at": _now_iso(),
                    "move_files": move_files,
                    "fingerprint": fingerprint,
                    "model_dir": self._model_dir_string(model_root),
                },
                limit=ORGANIZER_TASK_LIMIT,
            )

            preview_paths = self._extract_preview_images(target_file, images_dir)
            meta = self._build_meta(
                model_root=model_root,
                title=source_title,
                source_relative_path=relative_source,
                original_filename=source_path.name,
                target_file=target_file,
                move_files=move_files,
                fingerprint=fingerprint,
                preview_paths=preview_paths,
            )
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
                    "message": "本地 3MF 已整理完成。",
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
            )
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

    def _prepare_model_root(self, library_root: Path, source_path: Path, *, existing: dict) -> Path:
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
        file_name = sanitize_filename(source_path.name) or "model.3mf"
        suffix = Path(file_name).suffix or ".3mf"
        stem = Path(file_name).stem or "model"
        target = instances_dir / f"{stem}{suffix}"
        index = 2
        while target.exists():
            target = instances_dir / f"{stem}_{index}{suffix}"
            index += 1

        _ensure_parent(target)
        if move_files:
            shutil.move(str(source_path), str(target))
        else:
            shutil.copy2(source_path, target)
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
                    while candidate_name in written_names:
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
    ) -> dict:
        now_iso = _now_iso()
        try:
            publish_iso = datetime.fromtimestamp(target_file.stat().st_mtime).isoformat(timespec="seconds")
        except OSError:
            publish_iso = now_iso

        cover_path = preview_paths[0] if preview_paths else ""
        gallery_items = [{"relPath": item} for item in preview_paths]

        return {
            "id": "",
            "title": title,
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
                "name": "本地整理",
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
                    "name": title,
                    "machine": "本地 3MF",
                    "publishedAt": publish_iso,
                    "summary": "该文件来自本地整理目录，可直接下载 3MF。",
                    "thumbnailLocal": cover_path,
                    "pictures": gallery_items,
                    "fileName": target_file.name,
                    "downloadCount": 0,
                    "printCount": 0,
                    "plateCount": 0,
                }
            ],
            "localImport": {
                "sourcePath": source_relative_path,
                "originalFilename": original_filename,
                "organizedAt": now_iso,
                "moveFiles": move_files,
                "fingerprint": fingerprint,
            },
        }

    def _model_dir_string(self, model_root: Optional[Path]) -> str:
        if not model_root:
            return ""
        try:
            return model_root.resolve().relative_to(ARCHIVE_DIR.resolve()).as_posix()
        except ValueError:
            return model_root.name
