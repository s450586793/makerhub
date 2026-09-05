from typing import Any

from app.services.batch_discovery import discover_batch_model_urls
from app.services.legacy_archiver import archive_model as legacy_archive_model

from .source_status import source_is_deleted


def archive_model(**kwargs: Any) -> dict[str, Any]:
    return legacy_archive_model(**kwargs)


def discover_source(url: str, raw_cookie: str, max_pages: int = 12) -> dict[str, Any]:
    return discover_batch_model_urls(url, raw_cookie, max_pages=max_pages)


__all__ = ["archive_model", "discover_source", "source_is_deleted"]
