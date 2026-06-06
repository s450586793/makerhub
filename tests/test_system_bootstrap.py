import asyncio
from types import SimpleNamespace
from unittest.mock import patch

from app.api import config as config_api
from app.api import system


def test_bootstrap_uses_session_snapshot_without_slow_system_checks():
    request = SimpleNamespace(
        state=SimpleNamespace(
            auth_identity={
                "kind": "session",
                "username": "admin",
                "session_id": "session-1",
            }
        )
    )

    with (
        patch.object(config_api.store, "load", side_effect=AssertionError("bootstrap must not load full config")),
        patch.object(config_api, "_get_github_version_status", side_effect=AssertionError("bootstrap must not check GitHub")),
    ):
        payload = asyncio.run(system.get_bootstrap(request))

    assert not hasattr(system, "database_status")
    assert payload["session"] == {
        "authenticated": True,
        "kind": "session",
        "username": "admin",
        "display_name": "",
    }
    assert payload["app_version"]
    assert "database" not in payload
    assert "github_latest_version" not in payload
