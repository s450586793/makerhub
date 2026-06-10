from __future__ import annotations

from typing import Any

from app.services.process_jobs import run_archive_model_job


def run_source_refresh_model_job(
    *,
    url: str,
    cookie: str,
    download_dir: str,
    logs_dir: str,
    existing_root: str = "",
    progress_callback: Any = None,
    three_mf_skip_message: str = "",
    three_mf_skip_state: str = "pending_download",
    download_assets: bool = False,
    download_comment_assets: bool = False,
    proxy_config: Any = None,
) -> dict[str, Any]:
    return run_archive_model_job(
        url=url,
        cookie=cookie,
        download_dir=download_dir,
        logs_dir=logs_dir,
        existing_root=existing_root,
        progress_callback=progress_callback,
        skip_three_mf_fetch=True,
        three_mf_skip_message=three_mf_skip_message,
        three_mf_skip_state=three_mf_skip_state,
        download_assets=bool(download_assets),
        download_comment_assets=bool(download_comment_assets),
        rebuild_archive=False,
        record_missing_3mf_log=False,
        proxy_config=proxy_config,
    )

