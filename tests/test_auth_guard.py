import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import Optional
from unittest.mock import patch

from fastapi import HTTPException

from app.core.security import hash_api_token
from app.core.store import JsonStore
from app.schemas.models import ApiTokenRecord, AppConfig
from app.core.api_permissions import api_token_permission_for_request, is_public_api_route, is_session_only_api_route
from app.services.auth import AuthManager
from app import main as main_app
from app.api import auth as auth_api


def _token_record(raw_token: str, permissions: list[str]) -> ApiTokenRecord:
    return ApiTokenRecord(
        id=f"token-{permissions[0]}",
        name="test",
        token_prefix=raw_token[:12],
        token_hash=hash_api_token(raw_token),
        token_value=raw_token,
        permissions=permissions,
        created_at="2026-05-19T10:00:00+08:00",
    )


class AuthGuardTokenPermissionTest(unittest.TestCase):
    def setUp(self):
        self.sessions_state = {}
        self.auth_patches = [
            patch("app.services.auth.load_database_json_state", side_effect=lambda _key, default: dict(self.sessions_state or default)),
            patch("app.services.auth.save_database_json_state", side_effect=lambda _key, value: self.sessions_state.clear() or self.sessions_state.update(value) or value),
        ]
        for item in self.auth_patches:
            item.start()

    def tearDown(self):
        for item in reversed(self.auth_patches):
            item.stop()

    def _request(self, raw_token: str):
        return SimpleNamespace(
            cookies={},
            headers={"Authorization": f"Bearer {raw_token}"},
            query_params={},
        )

    def test_route_permission_mapping_keeps_sensitive_config_session_only(self):
        self.assertEqual(api_token_permission_for_request("POST", "/api/archive"), "archive_write")
        self.assertEqual(api_token_permission_for_request("GET", "/api/models"), "models_read")
        self.assertEqual(api_token_permission_for_request("GET", "/api/config"), "")
        self.assertEqual(api_token_permission_for_request("POST", "/api/config/cookies"), "")
        self.assertTrue(is_session_only_api_route("GET", "/api/config"))
        self.assertTrue(is_session_only_api_route("POST", "/api/config/cookies"))

    def test_every_api_route_has_explicit_auth_policy(self):
        unresolved = []
        for route in main_app.app.routes:
            path = getattr(route, "path", "")
            methods = getattr(route, "methods", set()) or set()
            if not str(path).startswith("/api/"):
                continue
            for method in sorted(methods - {"HEAD", "OPTIONS"}):
                if (
                    is_public_api_route(path)
                    or api_token_permission_for_request(method, path)
                    or is_session_only_api_route(method, path)
                ):
                    continue
                unresolved.append(f"{method} {path}")

        self.assertEqual(unresolved, [])

    def test_archive_token_cannot_be_used_as_global_api_token(self):
        with tempfile.TemporaryDirectory() as tmp:
            raw_token = "mht_archive_token"
            store = JsonStore(Path(tmp) / "config.json")
            config = AppConfig()
            config.api_tokens = [_token_record(raw_token, ["archive_write"])]
            store.save(config)
            manager = AuthManager(store=store, sessions_path=Path(tmp) / "sessions.json")

            self.assertIsNotNone(
                manager.resolve_request_auth(
                    self._request(raw_token),
                    api_token_permission="archive_write",
                )
            )
            self.assertIsNone(
                manager.resolve_request_auth(
                    self._request(raw_token),
                    api_token_permission="models_read",
                )
            )
            self.assertIsNone(manager.resolve_request_auth(self._request(raw_token)))

    def test_models_read_token_allows_only_model_read_permission(self):
        with tempfile.TemporaryDirectory() as tmp:
            raw_token = "mht_models_token"
            store = JsonStore(Path(tmp) / "config.json")
            config = AppConfig()
            config.api_tokens = [_token_record(raw_token, ["models_read"])]
            store.save(config)
            manager = AuthManager(store=store, sessions_path=Path(tmp) / "sessions.json")

            identity = manager.resolve_request_auth(
                self._request(raw_token),
                api_token_permission="models_read",
            )
            self.assertIsNotNone(identity)
            self.assertEqual(identity["kind"], "token")
            self.assertIn("models_read", identity["token"]["permissions"])
            self.assertIsNone(
                manager.resolve_request_auth(
                    self._request(raw_token),
                    api_token_permission="archive_write",
                )
            )


class AuthLoginHardeningTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.sessions_state = {}
        self.auth_patches = [
            patch("app.services.auth.load_database_json_state", side_effect=lambda _key, default: dict(self.sessions_state or default)),
            patch("app.services.auth.save_database_json_state", side_effect=lambda _key, value: self.sessions_state.clear() or self.sessions_state.update(value) or value),
        ]
        for item in self.auth_patches:
            item.start()

    def tearDown(self):
        for item in reversed(self.auth_patches):
            item.stop()

    def _login_request(self, *, scheme: str = "http", headers: Optional[dict] = None, host: str = "127.0.0.1"):
        return SimpleNamespace(
            headers=headers or {},
            client=SimpleNamespace(host=host),
            url=SimpleNamespace(scheme=scheme),
        )

    async def test_login_rate_limits_repeated_failures_and_clears_after_success(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = JsonStore(Path(tmp) / "config.json")
            manager = AuthManager(store=store, sessions_path=Path(tmp) / "sessions.json")
            with patch.object(auth_api, "store", store), \
                    patch.object(auth_api, "auth_manager", manager), \
                    patch.object(auth_api, "append_business_log"):
                request = self._login_request()
                payload = SimpleNamespace(username="admin", password="wrong")
                for _ in range(5):
                    with self.assertRaises(HTTPException) as failed:
                        await auth_api.login(payload, request)
                    self.assertEqual(failed.exception.status_code, 401)

                key = manager.login_failure_key(request, "admin")
                self.assertGreater(manager.login_backoff_seconds(key), 0)

                with self.assertRaises(HTTPException) as rate_limited:
                    await auth_api.login(SimpleNamespace(username="admin", password="admin"), request)
                self.assertEqual(rate_limited.exception.status_code, 429)

                manager.clear_login_failures(key)
                response = await auth_api.login(SimpleNamespace(username="admin", password="admin"), request)
                self.assertEqual(response.status_code, 200)
                self.assertEqual(manager.login_backoff_seconds(key), 0)

    async def test_login_sets_secure_cookie_behind_https_proxy_and_reports_default_password(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = JsonStore(Path(tmp) / "config.json")
            manager = AuthManager(store=store, sessions_path=Path(tmp) / "sessions.json")
            with patch.object(auth_api, "store", store), \
                    patch.object(auth_api, "auth_manager", manager), \
                    patch.object(auth_api, "append_business_log"):
                response = await auth_api.login(
                    SimpleNamespace(username="admin", password="admin"),
                    self._login_request(headers={"X-Forwarded-Proto": "https"}),
                )

            cookie = response.headers.get("set-cookie", "")
            self.assertIn("Secure", cookie)
            self.assertIn('"default_password":true', response.body.decode("utf-8"))


if __name__ == "__main__":
    unittest.main()
