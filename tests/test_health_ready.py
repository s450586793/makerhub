import asyncio
import json
from unittest.mock import patch

from app.api import system
from app.services import self_update


def test_public_readiness_returns_sanitized_503_when_database_is_unavailable():
    database_url = "postgresql://makerhub:top-secret@postgres:5432/makerhub"
    with patch.object(
        system.database,
        "database_status",
        return_value={"available": False, "error": database_url},
    ):
        response = asyncio.run(system.public_health_ready())

    payload = json.loads(response.body)
    assert response.status_code == 503
    assert payload == {
        "ready": False,
        "role": system.PROCESS_ROLE,
        "version": system.APP_VERSION,
        "database": {"ready": False, "schema_version": 0, "expected_schema_version": 0},
    }
    assert database_url not in response.body.decode("utf-8")
    assert "secret" not in response.body.decode("utf-8")


def test_public_readiness_returns_database_version_and_role_when_ready():
    with patch.object(
        system.database,
        "database_status",
        return_value={
            "available": True,
            "schema_version": 3,
            "expected_schema_version": 3,
        },
    ):
        response = asyncio.run(system.public_health_ready())

    assert response.status_code == 200
    assert json.loads(response.body) == {
        "ready": True,
        "role": system.PROCESS_ROLE,
        "version": system.APP_VERSION,
        "database": {"ready": True, "schema_version": 3, "expected_schema_version": 3},
    }


def test_worker_heartbeat_records_version_token_and_rejects_stale_or_wrong_token():
    saved = {}
    with patch.object(
        self_update,
        "save_database_json_state",
        side_effect=lambda key, value: saved.update(key=key, value=value) or value,
    ):
        heartbeat = self_update.record_worker_heartbeat(
            start_token="candidate-token",
            version="0.11.0",
            now_epoch=100.0,
        )

    assert saved["key"] == self_update.WORKER_HEARTBEAT_STATE_KEY
    assert heartbeat["start_token"] == "candidate-token"
    assert heartbeat["version"] == "0.11.0"
    assert heartbeat["updated_at_epoch"] == 100.0
    with patch.object(self_update, "load_database_json_state", return_value=heartbeat):
        stale = self_update.worker_heartbeat_readiness(
            expected_start_token="candidate-token",
            expected_version="0.11.0",
            now_epoch=140.0,
            max_age_seconds=30,
        )
        wrong_token = self_update.worker_heartbeat_readiness(
            expected_start_token="other-token",
            expected_version="0.11.0",
            now_epoch=110.0,
            max_age_seconds=30,
        )

    assert stale == {"ready": False, "reason": "stale"}
    assert wrong_token == {"ready": False, "reason": "start_token_mismatch"}
