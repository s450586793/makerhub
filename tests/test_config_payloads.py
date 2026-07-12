import asyncio
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from app.api import config as config_api
from app.core.security import hash_api_token
from app.core.store import JsonStore
from app.schemas.models import ApiTokenRecord, AppConfig
from app.services.auth import AuthManager


def test_public_config_light_payload_excludes_heavy_runtime_sections():
    with (
        patch.object(config_api, "database_status", side_effect=AssertionError("light payload must not check database")),
        patch.object(config_api, "cookie_source_inventory_payload", side_effect=AssertionError("light payload must not load cookie inventory")),
        patch.object(config_api, "cookie_source_sync_state_payload", side_effect=AssertionError("light payload must not load cookie sync state")),
        patch.object(config_api.task_state_store, "load_remote_refresh_state", side_effect=AssertionError("light payload must not load remote refresh state")),
    ):
        payload = config_api._public_config_light_payload(AppConfig())

    assert payload["app_version"]
    assert "cookies" in payload
    assert "proxy" in payload
    assert "user" in payload
    assert "subscriptions" in payload
    assert "database" not in payload
    assert "remote_refresh_state" not in payload
    assert "cookie_source_inventory" not in payload
    assert "cookie_source_sync_state" not in payload


def test_mobile_import_token_reset_uses_atomic_updates_without_stale_snapshot_save():
    with tempfile.TemporaryDirectory() as tmp:
        store = JsonStore(Path(tmp) / "config.json")
        manager = AuthManager(store=store, sessions_path=Path(tmp) / "sessions.json")
        request = SimpleNamespace(state=SimpleNamespace(auth_identity={"kind": "session"}))
        config = store.load()
        config.api_tokens = [
            ApiTokenRecord(
                id="old-mobile-1",
                name="old mobile 1",
                token_prefix="mhi_test",
                created_at="2026-07-12T10:00:00+08:00",
                token_hash=hash_api_token("mhi_old_1"),
                permissions=["mobile_import"],
            ),
            ApiTokenRecord(
                id="old-mobile-2",
                name="old mobile 2",
                token_prefix="mhi_test",
                created_at="2026-07-12T10:00:00+08:00",
                token_hash=hash_api_token("mhi_old_2"),
                permissions=["archive_write", "mobile_import"],
            ),
            ApiTokenRecord(
                id="archive-only",
                name="archive",
                token_prefix="mhi_test",
                created_at="2026-07-12T10:00:00+08:00",
                token_hash=hash_api_token("mht_archive"),
                permissions=["archive_write"],
            ),
        ]
        store.save(config)

        with (
            patch.object(config_api, "store", store),
            patch.object(config_api, "auth_manager", manager),
            patch.object(store, "save", side_effect=AssertionError("token/config writes must use JsonStore.update")),
            patch.object(store, "update", wraps=store.update) as atomic_update,
            patch.object(config_api, "append_business_log"),
            patch.object(config_api, "_public_config_payload", side_effect=lambda config: {
                "mobile_import": config.mobile_import.model_dump(exclude={"token_hash"}),
                "api_tokens": [manager._token_view(item).model_dump() for item in config.api_tokens],
            }),
            patch.object(config_api, "_get_github_version_status", new=AsyncMock(return_value={})),
        ):
            response = asyncio.run(
                config_api.reset_mobile_import_token(
                    SimpleNamespace(enabled=True),
                    request,
                )
            )

        assert atomic_update.call_count == 1
        raw_token = response["token"]
        saved = store.load()
        assert saved.mobile_import.enabled is True
        assert saved.mobile_import.token_hash == hash_api_token(raw_token)
        assert any(item.token_hash == hash_api_token(raw_token) for item in saved.api_tokens)
        old_mobile = [item for item in saved.api_tokens if item.id.startswith("old-mobile-")]
        assert old_mobile and all(item.disabled and item.revoked_at for item in old_mobile)
        assert next(item for item in saved.api_tokens if item.id == "archive-only").disabled is False
        assert all(not item.token_value for item in saved.api_tokens)
        assert raw_token not in store.path.read_text(encoding="utf-8")

def test_mobile_import_disable_revokes_every_mobile_import_token():
    with tempfile.TemporaryDirectory() as tmp:
        store = JsonStore(Path(tmp) / "config.json")
        manager = AuthManager(store=store, sessions_path=Path(tmp) / "sessions.json")
        request = SimpleNamespace(state=SimpleNamespace(auth_identity={"kind": "session"}))
        config = store.load()
        config.mobile_import.enabled = True
        config.api_tokens = [
            ApiTokenRecord(
                id="mobile-a",
                name="mobile a",
                token_prefix="mhi_test",
                created_at="2026-07-12T10:00:00+08:00",
                token_hash=hash_api_token("mhi_a"),
                permissions=["mobile_import"],
            ),
            ApiTokenRecord(
                id="mobile-b",
                name="mobile b",
                token_prefix="mhi_test",
                created_at="2026-07-12T10:00:00+08:00",
                token_hash=hash_api_token("mhi_b"),
                permissions=["archive_write", "mobile_import"],
            ),
            ApiTokenRecord(
                id="archive-only",
                name="archive",
                token_prefix="mhi_test",
                created_at="2026-07-12T10:00:00+08:00",
                token_hash=hash_api_token("mht_archive"),
                permissions=["archive_write"],
            ),
        ]
        store.save(config)

        with (
            patch.object(config_api, "store", store),
            patch.object(config_api, "auth_manager", manager),
            patch.object(config_api, "append_business_log"),
            patch.object(config_api, "_public_config_payload", side_effect=lambda value: {}),
            patch.object(config_api, "_get_github_version_status", new=AsyncMock(return_value={})),
        ):
            asyncio.run(config_api.disable_mobile_import(request))

        saved = store.load()
        assert saved.mobile_import.enabled is False
        assert all(item.disabled and item.revoked_at for item in saved.api_tokens if "mobile_import" in item.permissions)
        assert next(item for item in saved.api_tokens if item.id == "archive-only").disabled is False
