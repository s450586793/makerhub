from unittest.mock import patch

from app.api import config as config_api
from app.schemas.models import AppConfig


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
