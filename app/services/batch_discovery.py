"""MakerWorld discovery compatibility facade."""

from app.services.makerworld_parsers.common import extract_model_id, normalize_model_url, normalize_source_url
from app.services.makerworld_pipeline.discovery import (
    default_favorites_source as default_favorites_subscription_source,
    discover_account_home_summary as discover_cookie_account_home_summary,
    discover_account_profile as discover_cookie_account_profile,
    discover_followed_authors as discover_cookie_followed_authors,
    discover_followed_authors_from_page as discover_cookie_followed_authors_from_page,
    discover_followed_collections as discover_cookie_followed_collections,
    discover_source as discover_batch_model_urls,
    resolve_source_name as resolve_batch_source_name,
)

__all__ = [
    "default_favorites_subscription_source",
    "discover_batch_model_urls",
    "discover_cookie_account_home_summary",
    "discover_cookie_account_profile",
    "discover_cookie_followed_authors",
    "discover_cookie_followed_authors_from_page",
    "discover_cookie_followed_collections",
    "extract_model_id",
    "normalize_model_url",
    "normalize_source_url",
    "resolve_batch_source_name",
]
