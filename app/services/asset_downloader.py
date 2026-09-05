import json
import os
import threading
import time
import zipfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Callable
from urllib.parse import urlsplit, urlunsplit

import requests

from app.core.timezone import now_iso as china_now_iso
from app.services.resource_limiter import resource_slot


CONNECT_TIMEOUT_SECONDS = 15
READ_TIMEOUT_SECONDS = 30
IMAGE_TRANSFER_TIMEOUT_SECONDS = 45
FAKE_THREE_MF_DOWNLOAD_ENV = "MAKERHUB_FAKE_THREE_MF_DOWNLOADS"
FAKE_THREE_MF_DOWNLOAD_URL_PREFIX = "makerhub://fake-3mf/"
FAKE_THREE_MF_DOWNLOAD_MESSAGE = "本地 Docker 已启用 3MF 假下载，不会请求 MakerWorld 实际文件。"


class AssetDownloadError(RuntimeError):
    pass


def log(*args):
    print("[MW-FETCH]", *args)


def safe_asset_url(url: str) -> str:
    parsed = urlsplit(str(url or ""))
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "", ""))


def fake_three_mf_downloads_enabled() -> bool:
    return str(os.getenv(FAKE_THREE_MF_DOWNLOAD_ENV, "")).strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
        "enabled",
    }


def _is_three_mf_fake_download_target(url: str, dest: Path) -> bool:
    if str(url or "").startswith(FAKE_THREE_MF_DOWNLOAD_URL_PREFIX):
        return True
    return dest.suffix.lower() == ".3mf"


def _write_fake_three_mf_file(dest: Path, source_url: str) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    temp_dest = dest.with_name(f"{dest.name}.{os.getpid()}.{threading.get_ident()}.part")
    payload = {
        "fake": True,
        "generator": "MakerHub",
        "source_url": str(source_url or ""),
        "message": FAKE_THREE_MF_DOWNLOAD_MESSAGE,
        "generated_at": china_now_iso(),
    }
    model_xml = """<?xml version="1.0" encoding="UTF-8"?>
<model unit="millimeter" xml:lang="zh-CN" xmlns="http://schemas.microsoft.com/3dmanufacturing/core/2015/02">
  <metadata name="Title">MakerHub fake 3MF placeholder</metadata>
  <metadata name="MakerHubFakeDownload">true</metadata>
  <resources/>
  <build/>
</model>
"""
    rels_xml = """<?xml version="1.0" encoding="UTF-8"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Target="/3D/3dmodel.model" Id="rel0" Type="http://schemas.microsoft.com/3dmanufacturing/2013/01/3dmodel"/>
</Relationships>
"""
    content_types_xml = """<?xml version="1.0" encoding="UTF-8"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
  <Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
  <Default Extension="model" ContentType="application/vnd.ms-package.3dmanufacturing-3dmodel+xml"/>
  <Default Extension="json" ContentType="application/json"/>
</Types>
"""
    try:
        with zipfile.ZipFile(temp_dest, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("[Content_Types].xml", content_types_xml)
            archive.writestr("_rels/.rels", rels_xml)
            archive.writestr("3D/3dmodel.model", model_xml)
            archive.writestr(
                "Metadata/makerhub_fake_download.json",
                json.dumps(payload, ensure_ascii=False, indent=2),
            )
        temp_dest.replace(dest)
    except Exception:
        try:
            if temp_dest.exists():
                temp_dest.unlink()
        except Exception:
            pass
        raise


def download_file(
    session: requests.Session,
    url: str,
    dest: Path,
    overwrite: bool = False,
    *,
    timeout: tuple[int, int] = (CONNECT_TIMEOUT_SECONDS, READ_TIMEOUT_SECONDS),
    max_duration: int = IMAGE_TRANSFER_TIMEOUT_SECONDS,
) -> None:
    if dest.exists() and not overwrite:
        log("存在，跳过：", dest)
        return
    if fake_three_mf_downloads_enabled() and _is_three_mf_fake_download_target(url, dest):
        _write_fake_three_mf_file(dest, url)
        log("[3MF] 假下载已写入：", dest)
        return
    dest.parent.mkdir(parents=True, exist_ok=True)
    temp_dest = dest.with_name(f"{dest.name}.{os.getpid()}.{threading.get_ident()}.part")
    started_at = time.monotonic()
    log("开始下载：", safe_asset_url(url), "->", dest)
    try:
        with session.get(url, timeout=timeout, stream=True) as resp:
            resp.raise_for_status()
            with temp_dest.open("wb") as f:
                for chunk in resp.iter_content(chunk_size=64 * 1024):
                    if max_duration > 0 and time.monotonic() - started_at > max_duration:
                        raise TimeoutError(f"下载超时（>{max_duration}s）: {url}")
                    if not chunk:
                        continue
                    f.write(chunk)
        temp_dest.replace(dest)
    except Exception as exc:
        try:
            if temp_dest.exists():
                temp_dest.unlink()
        except Exception:
            pass
        if isinstance(exc, (requests.RequestException, TimeoutError)):
            raise AssetDownloadError(f"静态资源下载失败：{safe_asset_url(url)}") from exc
        raise
    log("已下载：", dest)


def download_with_fresh_session(
    base_session: requests.Session,
    url: str,
    dest: Path,
    *,
    download_func: Callable[..., None] | None = None,
) -> None:
    active_download = download_func or download_file
    with resource_slot("comment_assets", detail=safe_asset_url(url)):
        if type(base_session) is not requests.Session:
            active_download(
                base_session,
                url,
                dest,
                overwrite=True,
                max_duration=IMAGE_TRANSFER_TIMEOUT_SECONDS,
            )
            return
        with requests.Session() as asset_session:
            asset_session.headers.update(getattr(base_session, "headers", {}) or {})
            asset_session.cookies.update(getattr(base_session, "cookies", {}) or {})
            active_download(
                asset_session,
                url,
                dest,
                overwrite=True,
                max_duration=IMAGE_TRANSFER_TIMEOUT_SECONDS,
            )


def run_asset_tasks(
    tasks: list[dict],
    *,
    max_workers: int,
    on_progress: Callable[[int, int], None] | None = None,
    on_error: Callable[[dict, Exception], None] | None = None,
) -> dict[str, int]:
    stats = {"completed": 0, "failed": 0}
    if not tasks:
        return stats
    with ThreadPoolExecutor(max_workers=max(1, min(int(max_workers), len(tasks)))) as executor:
        future_map = {executor.submit(task["download"]): task for task in tasks}
        for completed, future in enumerate(as_completed(future_map), start=1):
            task = future_map[future]
            if on_progress:
                on_progress(completed, len(tasks))
            try:
                future.result()
            except Exception as exc:
                stats["failed"] += 1
                if on_error:
                    on_error(task, exc)
                continue
            stats["completed"] += 1
            for apply_ref in task.get("apply") or []:
                try:
                    apply_ref()
                except Exception:
                    continue
    return stats
