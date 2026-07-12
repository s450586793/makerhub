import json
import os
import subprocess
import tempfile
import threading
import unittest
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
from typing import Optional
from unittest.mock import patch

from fastapi import HTTPException

from app.core.database import DatabaseUnavailable
from app.core.security import default_admin_password_hash, hash_api_token, hash_password
from app.core.store import JsonStore
from app.schemas.models import ApiTokenRecord, AppConfig
from app.core.api_permissions import api_token_permission_for_request, is_public_api_route, is_session_only_api_route
from app.services import auth as auth_service
from app.services.auth import AuthManager
from app import main as main_app
from app.api import auth as auth_api


class _InMemoryJsonState:
    def __init__(self):
        self.values = {}
        self.revisions = {}
        self.lock = threading.RLock()

    def load(self, key, default):
        with self.lock:
            return deepcopy(self.values.get(key, default))

    def update(self, key, default, mutator, *, expected_revision=None):
        with self.lock:
            revision = self.revisions.get(key, 0)
            if expected_revision is not None and expected_revision != revision:
                raise RuntimeError("stale revision")
            current = deepcopy(self.values.get(key, default))
            result = mutator(current)
            updated = current if result is None else result
            revision += 1
            self.values[key] = deepcopy(updated)
            self.revisions[key] = revision
            return deepcopy(updated), revision


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
        self.json_state = _InMemoryJsonState()
        self.auth_patches = [
            patch("app.services.auth.load_database_json_state", side_effect=self.json_state.load),
            patch(
                "app.services.auth.update_database_json_state",
                side_effect=self.json_state.update,
                create=True,
            ),
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

    def test_api_token_query_parameter_is_not_accepted(self):
        with tempfile.TemporaryDirectory() as tmp:
            raw_token = "mht_archive_token"
            store = JsonStore(Path(tmp) / "config.json")
            config = AppConfig()
            config.api_tokens = [_token_record(raw_token, ["archive_write"])]
            store.save(config)
            manager = AuthManager(store=store, sessions_path=Path(tmp) / "sessions.json")
            request = SimpleNamespace(
                cookies={},
                headers={},
                query_params={"token": raw_token},
            )

            self.assertIsNone(
                manager.resolve_request_auth(
                    request,
                    api_token_permission="archive_write",
                )
            )

    def test_api_token_plaintext_is_returned_once_but_never_persisted_or_listed(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = JsonStore(Path(tmp) / "config.json")
            manager = AuthManager(store=store, sessions_path=Path(tmp) / "sessions.json")

            raw_token, created = manager.create_api_token("automation", permissions=["archive_write"])
            saved = store.load()
            listed = manager.list_api_tokens()

            self.assertTrue(raw_token.startswith("mht_"))
            self.assertEqual(created.token_value, raw_token)
            self.assertFalse(saved.api_tokens[0].token_value)
            self.assertFalse(listed[0].token_value)
            self.assertNotIn(raw_token, store.path.read_text(encoding="utf-8"))
            self.assertIsNotNone(
                manager.resolve_request_auth(
                    self._request(raw_token),
                    api_token_permission="archive_write",
                )
            )

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

    def test_session_auth_uses_short_memory_cache_and_invalidates_on_delete(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = JsonStore(Path(tmp) / "config.json")
            manager = AuthManager(store=store, sessions_path=Path(tmp) / "sessions.json")
            session = manager.create_session("admin")
            request = SimpleNamespace(
                cookies={"makerhub_session": session["id"]},
                headers={},
                query_params={},
            )
            read_calls = 0
            original_read_sessions = manager._read_sessions

            def counted_read_sessions():
                nonlocal read_calls
                read_calls += 1
                return original_read_sessions()

            with patch.object(manager, "_read_sessions", side_effect=counted_read_sessions):
                first = manager.resolve_request_auth(request)
                second = manager.resolve_request_auth(request)
                reads_before_delete = read_calls
                manager.delete_session(session["id"])
                third = manager.resolve_request_auth(request)

            self.assertEqual(first["username"], "admin")
            self.assertEqual(second["username"], "admin")
            self.assertIsNone(third)
            self.assertEqual(reads_before_delete, 2)
            self.assertEqual(read_calls, 3)

    def test_session_cache_invalidation_is_shared_across_auth_manager_instances(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = JsonStore(Path(tmp) / "config.json")
            resolver = AuthManager(store=store, sessions_path=Path(tmp) / "sessions.json")
            deleter = AuthManager(store=store, sessions_path=Path(tmp) / "sessions.json")
            session = deleter.create_session("admin")
            request = SimpleNamespace(
                cookies={"makerhub_session": session["id"]},
                headers={},
                query_params={},
            )

            first = resolver.resolve_request_auth(request)
            with patch.object(deleter, "_clear_session_cache"):
                deleter.delete_session(session["id"])
            second = resolver.resolve_request_auth(request)

            self.assertEqual(first["username"], "admin")
            self.assertIsNone(second)

    def test_create_session_persists_exactly_one_record(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = JsonStore(Path(tmp) / "config.json")
            manager = AuthManager(store=store, sessions_path=Path(tmp) / "sessions.json")

            session = manager.create_session("admin")

        stored = self.json_state.values[auth_service.SESSIONS_STATE_KEY]
        self.assertEqual(stored["items"], [session])
        self.assertEqual(stored["generation"], 1)

    def test_concurrent_session_clear_cannot_be_overwritten_by_stale_get_touch(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = JsonStore(Path(tmp) / "config.json")
            resolver = AuthManager(store=store, sessions_path=Path(tmp) / "sessions.json")
            clearer = AuthManager(store=store, sessions_path=Path(tmp) / "sessions.json")
            session = resolver.create_session("admin")
            stale_payload = deepcopy(self.json_state.values[auth_service.SESSIONS_STATE_KEY])
            stale_payload["items"][0]["last_seen_at"] = ""

            with patch.object(clearer, "_clear_session_cache"):
                clearer.clear_all_sessions()
            cleared_generation = int(
                self.json_state.values[auth_service.SESSIONS_STATE_KEY].get("generation", 0)
            )
            with patch.object(resolver, "_read_sessions", return_value=stale_payload):
                resolved = resolver.get_session(session["id"])

            self.assertIsNone(resolved)
            self.assertEqual(self.json_state.values[auth_service.SESSIONS_STATE_KEY]["items"], [])
            self.assertGreaterEqual(
                int(self.json_state.values[auth_service.SESSIONS_STATE_KEY].get("generation", 0)),
                cleared_generation,
            )

    def test_legacy_plaintext_token_is_hashed_and_removed_on_load(self):
        with tempfile.TemporaryDirectory() as tmp:
            raw_token = "mht_legacy_plaintext_token"
            path = Path(tmp) / "config.json"
            payload = AppConfig().model_dump()
            payload["api_tokens"] = [
                {
                    "id": "legacy-token",
                    "name": "legacy",
                    "token_prefix": raw_token[:12],
                    "token_value": raw_token,
                    "created_at": "2026-05-19T10:00:00+08:00",
                    "permissions": ["archive_write"],
                }
            ]
            path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
            store = JsonStore(path)
            manager = AuthManager(store=store, sessions_path=Path(tmp) / "sessions.json")

            listed = manager.list_api_tokens()
            saved = store.load()

            self.assertEqual(saved.api_tokens[0].token_hash, hash_api_token(raw_token))
            self.assertFalse(saved.api_tokens[0].token_value)
            self.assertFalse(listed[0].token_value)
            self.assertNotIn(raw_token, path.read_text(encoding="utf-8"))
            self.assertIsNotNone(manager.validate_api_token(raw_token))


class AdminCredentialBootstrapTest(unittest.TestCase):
    def _store(self, root: Path) -> JsonStore:
        return JsonStore(root / "config.json")

    def test_fresh_config_never_accepts_shared_admin_password(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = self._store(root)
            config = store.load()
            config.user.password_hash = ""
            store.save(config)

            with patch.object(auth_service, "STATE_DIR", root / "state"), \
                    patch.dict(os.environ, {"MAKERHUB_ADMIN_PASSWORD": ""}), \
                    patch.object(AuthManager, "clear_all_sessions") as clear_sessions:
                result = auth_service.ensure_secure_admin_credential(store)

            bootstrap_password = result.bootstrap_path.read_text(encoding="utf-8").strip()
            self.assertGreaterEqual(len(bootstrap_password), 24)
            self.assertEqual(result.bootstrap_path.stat().st_mode & 0o077, 0)
            self.assertFalse(AuthManager(store).authenticate_credentials("admin", "admin"))
            self.assertTrue(AuthManager(store).authenticate_credentials("admin", bootstrap_password))
            self.assertNotIn(bootstrap_password, store.path.read_text(encoding="utf-8"))
            clear_sessions.assert_called_once_with()

    def test_valid_environment_password_bootstraps_without_plaintext_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = self._store(root)
            config = store.load()
            config.user.password_hash = default_admin_password_hash()
            store.save(config)
            env_password = "environment-password-123"

            with patch.object(auth_service, "STATE_DIR", root / "state"), \
                    patch.dict(os.environ, {"MAKERHUB_ADMIN_PASSWORD": env_password}), \
                    patch.object(AuthManager, "clear_all_sessions") as clear_sessions:
                result = auth_service.ensure_secure_admin_credential(store)

            self.assertTrue(AuthManager(store).authenticate_credentials("admin", env_password))
            self.assertFalse(AuthManager(store).authenticate_credentials("admin", "admin"))
            self.assertFalse(result.bootstrap_path.exists())
            self.assertNotIn(env_password, store.path.read_text(encoding="utf-8"))
            clear_sessions.assert_called_once_with()

    def test_generated_bootstrap_write_failure_keeps_existing_credential(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = self._store(root)
            config = store.load()
            config.user.password_hash = default_admin_password_hash()
            store.save(config)
            original_hash = store.load().user.password_hash

            with patch.object(auth_service, "STATE_DIR", root / "state"), \
                    patch.dict(os.environ, {"MAKERHUB_ADMIN_PASSWORD": ""}), \
                    patch.object(auth_service, "_write_bootstrap_password", side_effect=OSError("disk full")), \
                    patch.object(AuthManager, "clear_all_sessions") as clear_sessions:
                with self.assertRaisesRegex(OSError, "disk full"):
                    auth_service.ensure_secure_admin_credential(store)

            self.assertEqual(store.load().user.password_hash, original_hash)
            self.assertTrue(AuthManager(store).authenticate_credentials("admin", "admin"))
            clear_sessions.assert_not_called()

    def test_generated_bootstrap_survives_config_update_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = self._store(root)
            config = store.load()
            config.user.password_hash = default_admin_password_hash()
            store.save(config)

            with patch.object(auth_service, "STATE_DIR", root / "state"), \
                    patch.dict(os.environ, {"MAKERHUB_ADMIN_PASSWORD": ""}), \
                    patch.object(store, "update", side_effect=OSError("database offline")):
                with self.assertRaisesRegex(OSError, "database offline"):
                    auth_service.ensure_secure_admin_credential(store)

            bootstrap_path = root / "state" / auth_service.ADMIN_BOOTSTRAP_PASSWORD_FILENAME
            bootstrap_password = bootstrap_path.read_text(encoding="utf-8").strip()
            self.assertTrue(AuthManager(store).authenticate_credentials("admin", "admin"))

            with patch.object(auth_service, "STATE_DIR", root / "state"), \
                    patch.dict(os.environ, {"MAKERHUB_ADMIN_PASSWORD": ""}), \
                    patch.object(AuthManager, "clear_all_sessions"):
                auth_service.ensure_secure_admin_credential(store)

            self.assertTrue(AuthManager(store).authenticate_credentials("admin", bootstrap_password))


    def test_existing_secure_password_is_not_replaced_by_environment(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = self._store(root)
            current_password = "existing-secure-password"
            config = store.load()
            config.user.password_hash = hash_password(current_password)
            store.save(config)

            with patch.object(auth_service, "STATE_DIR", root / "state"), \
                    patch.dict(os.environ, {"MAKERHUB_ADMIN_PASSWORD": "different-environment-password"}), \
                    patch.object(AuthManager, "clear_all_sessions") as clear_sessions:
                result = auth_service.ensure_secure_admin_credential(store)

            manager = AuthManager(store)
            self.assertTrue(manager.authenticate_credentials("admin", current_password))
            self.assertFalse(manager.authenticate_credentials("admin", "different-environment-password"))
            self.assertFalse(result.rotated)
            clear_sessions.assert_not_called()

    def test_password_change_requires_twelve_characters_and_removes_bootstrap_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = self._store(root)
            config = store.load()
            config.user.password_hash = default_admin_password_hash()
            store.save(config)

            with patch.object(auth_service, "STATE_DIR", root / "state"), \
                    patch.dict(os.environ, {"MAKERHUB_ADMIN_PASSWORD": ""}), \
                    patch.object(AuthManager, "clear_all_sessions"):
                result = auth_service.ensure_secure_admin_credential(store)
                bootstrap_password = result.bootstrap_path.read_text(encoding="utf-8").strip()
                manager = AuthManager(store)
                with self.assertRaisesRegex(ValueError, "12"):
                    manager.change_password(bootstrap_password, "too-short")
                manager.change_password(bootstrap_password, "replacement-password")

            self.assertFalse(result.bootstrap_path.exists())
            self.assertTrue(AuthManager(store).authenticate_credentials("admin", "replacement-password"))


class AuthLoginHardeningTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.json_state = _InMemoryJsonState()
        self.auth_patches = [
            patch("app.services.auth.load_database_json_state", side_effect=self.json_state.load),
            patch(
                "app.services.auth.update_database_json_state",
                side_effect=self.json_state.update,
                create=True,
            ),
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

    def test_login_failure_count_is_shared_across_auth_manager_instances(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = JsonStore(Path(tmp) / "config.json")
            first = AuthManager(store=store, sessions_path=Path(tmp) / "sessions-1.json")
            second = AuthManager(store=store, sessions_path=Path(tmp) / "sessions-2.json")
            key = "127.0.0.1:admin"

            for _ in range(auth_service.LOGIN_FAILURE_LIMIT - 1):
                self.assertEqual(first.record_login_failure(key), 0)
            self.assertGreater(second.record_login_failure(key), 0)
            self.assertGreater(first.login_backoff_seconds(key), 0)
            second.clear_login_failures(key)
            self.assertEqual(first.login_backoff_seconds(key), 0)

    def test_login_failure_memory_fallback_prunes_expired_and_caps_keys(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = JsonStore(Path(tmp) / "config.json")
            manager = AuthManager(store=store, sessions_path=Path(tmp) / "sessions.json")
            now = auth_service.china_now().timestamp()
            manager._login_failures["expired"] = {
                "first_seen": now - 60,
                "last_seen": now - 60,
                "count": 1,
                "locked_until": 0,
            }

            with patch.object(
                    auth_service,
                    "update_database_json_state",
                    side_effect=DatabaseUnavailable("offline"),
            ), patch.object(
                auth_service,
                "load_database_json_state",
                side_effect=DatabaseUnavailable("offline"),
            ), patch.object(
                auth_service,
                "LOGIN_FAILURE_FALLBACK_TTL_SECONDS",
                10,
                create=True,
            ), patch.object(
                auth_service,
                "LOGIN_FAILURE_MAX_KEYS",
                2,
                create=True,
            ):
                manager.record_login_failure("key-1")
                manager.record_login_failure("key-2")
                manager.record_login_failure("key-3")

            self.assertNotIn("expired", manager._login_failures)
            self.assertLessEqual(len(manager._login_failures), 2)

    async def test_login_rate_limits_repeated_failures_and_clears_after_success(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = JsonStore(Path(tmp) / "config.json")
            config = store.load()
            config.user.password_hash = hash_password("valid-admin-password")
            store.save(config)
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
                    await auth_api.login(SimpleNamespace(username="admin", password="valid-admin-password"), request)
                self.assertEqual(rate_limited.exception.status_code, 429)

                manager.clear_login_failures(key)
                response = await auth_api.login(
                    SimpleNamespace(username="admin", password="valid-admin-password"),
                    request,
                )
                self.assertEqual(response.status_code, 200)
                self.assertEqual(manager.login_backoff_seconds(key), 0)

    async def test_spoofed_forwarding_headers_do_not_change_failure_key_or_cookie_security(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = JsonStore(Path(tmp) / "config.json")
            config = store.load()
            config.user.password_hash = hash_password("valid-admin-password")
            store.save(config)
            manager = AuthManager(store=store, sessions_path=Path(tmp) / "sessions.json")
            first_request = self._login_request(headers={"X-Forwarded-For": "198.51.100.1"})
            second_request = self._login_request(headers={"X-Forwarded-For": "203.0.113.9"})
            self.assertEqual(
                manager.login_failure_key(first_request, "Admin"),
                manager.login_failure_key(second_request, "admin"),
            )
            with patch.object(auth_api, "store", store), \
                    patch.object(auth_api, "auth_manager", manager), \
                    patch.object(auth_api, "append_business_log"):
                response = await auth_api.login(
                    SimpleNamespace(username="admin", password="valid-admin-password"),
                    self._login_request(headers={"X-Forwarded-Proto": "https", "X-Forwarded-Ssl": "on"}),
                )

            self.assertEqual(response.status_code, 200)
            self.assertNotIn("Secure", response.headers.get("set-cookie", ""))

    async def test_https_request_sets_secure_cookie(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = JsonStore(Path(tmp) / "config.json")
            config = store.load()
            config.user.password_hash = hash_password("valid-admin-password")
            store.save(config)
            manager = AuthManager(store=store, sessions_path=Path(tmp) / "sessions.json")
            with patch.object(auth_api, "store", store), \
                    patch.object(auth_api, "auth_manager", manager), \
                    patch.object(auth_api, "append_business_log"):
                response = await auth_api.login(
                    SimpleNamespace(username="admin", password="valid-admin-password"),
                    self._login_request(scheme="https"),
                )

            cookie = response.headers.get("set-cookie", "")
            self.assertIn("Secure", cookie)

    async def test_token_create_response_reveals_plaintext_once_but_list_does_not(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = JsonStore(Path(tmp) / "config.json")
            manager = AuthManager(store=store, sessions_path=Path(tmp) / "sessions.json")
            request = SimpleNamespace(state=SimpleNamespace(auth_identity={"kind": "session"}))
            payload = SimpleNamespace(name="CI", permissions=["archive_write"], expires_days=30)

            with patch.object(auth_api, "store", store), \
                    patch.object(auth_api, "auth_manager", manager), \
                    patch.object(auth_api, "append_business_log"):
                created = await auth_api.create_token(payload, request)
                listed = await auth_api.list_tokens(request)

            self.assertEqual(created["item"]["token_value"], created["token"])
            self.assertTrue(created["token"].startswith("mht_"))
            self.assertTrue(all(not item.get("token_value") for item in created["items"]))
            self.assertTrue(all(not item.get("token_value") for item in listed["items"]))


class CsrfOriginGuardTest(unittest.TestCase):
    def _request(self, *, origin: str = "", referer: str = "", host: str = "maker.example", scheme: str = "https"):
        headers = {"host": host}
        if origin:
            headers["origin"] = origin
        if referer:
            headers["referer"] = referer
        return SimpleNamespace(
            method="POST",
            headers=headers,
            url=SimpleNamespace(scheme=scheme),
        )

    def test_session_write_requires_same_origin_header(self):
        self.assertFalse(main_app._csrf_origin_is_valid(self._request()))
        self.assertFalse(main_app._csrf_origin_is_valid(self._request(origin="https://evil.example")))
        self.assertTrue(main_app._csrf_origin_is_valid(self._request(origin="https://maker.example")))
        self.assertTrue(main_app._csrf_origin_is_valid(self._request(referer="https://maker.example/settings")))

    def test_forwarded_proto_does_not_override_socket_scheme(self):
        request = self._request(origin="https://maker.example", scheme="http")
        request.headers["X-Forwarded-Proto"] = "https"

        self.assertFalse(main_app._csrf_origin_is_valid(request))


class EntrypointProxyHeaderTest(unittest.TestCase):
    def _entrypoint_args(self, root: Path, trusted_proxies: Optional[str] = None) -> list[str]:
        bin_dir = root / "bin"
        bin_dir.mkdir()
        capture_path = root / "uvicorn-args.txt"
        uvicorn_path = bin_dir / "uvicorn"
        uvicorn_path.write_text(
            '#!/bin/sh\nprintf "%s\\n" "$@" > "$MAKERHUB_TEST_CAPTURE"\n',
            encoding="utf-8",
        )
        uvicorn_path.chmod(0o755)
        env = os.environ.copy()
        env.update(
            {
                "PATH": f"{bin_dir}:{env.get('PATH', '')}",
                "MAKERHUB_ENTRYPOINT": "app",
                "MAKERHUB_TEST_CAPTURE": str(capture_path),
            }
        )
        if trusted_proxies is None:
            env.pop("MAKERHUB_TRUSTED_PROXIES", None)
        else:
            env["MAKERHUB_TRUSTED_PROXIES"] = trusted_proxies

        subprocess.run(
            [str(Path(__file__).resolve().parents[1] / "docker" / "entrypoint.sh")],
            check=True,
            cwd=Path(__file__).resolve().parents[1],
            env=env,
        )
        return capture_path.read_text(encoding="utf-8").splitlines()

    def test_proxy_headers_are_disabled_without_explicit_trusted_proxies(self):
        for value in (
            None,
            "",
            "*",
            "127.0.0.1,*",
            "*/8",
            "0.0.0.0/0",
            "::/0",
            "10.1.2.3/0",
            "127.0.0.1,0.0.0.0/00",
        ):
            with self.subTest(value=value), tempfile.TemporaryDirectory() as tmp:
                args = self._entrypoint_args(Path(tmp), value)
                self.assertIn("--no-proxy-headers", args)
                self.assertNotIn("--proxy-headers", args)

    def test_proxy_headers_are_enabled_only_for_explicit_trusted_proxies(self):
        with tempfile.TemporaryDirectory() as tmp:
            trusted = "127.0.0.1,10.0.0.0/8"
            args = self._entrypoint_args(Path(tmp), trusted)

        self.assertIn("--proxy-headers", args)
        self.assertIn("--forwarded-allow-ips", args)
        self.assertEqual(args[args.index("--forwarded-allow-ips") + 1], trusted)


class AppStartupCredentialTest(unittest.IsolatedAsyncioTestCase):
    async def test_app_startup_secures_the_global_admin_credential_first(self):
        calls = []
        with patch.object(
                main_app,
                "ensure_secure_admin_credential",
                side_effect=lambda _store: calls.append("credential") or auth_service.BootstrapCredentialResult(False, "existing", Path("/missing")),
        ) as ensure_credential, \
                patch.object(
                    main_app,
                    "mark_update_started_after_restart",
                    side_effect=lambda: calls.append("restart"),
                ) as mark_restart, \
                patch.object(
                    main_app,
                    "start_state_event_listener",
                    side_effect=lambda: calls.append("listener"),
                ) as start_listener, \
                patch.object(main_app, "BACKGROUND_TASKS_ENABLED", False), \
                patch.object(main_app, "append_business_log"):
            await main_app.resume_archive_queue()

        ensure_credential.assert_called_once_with(main_app.global_store)
        mark_restart.assert_called_once_with()
        start_listener.assert_called_once_with()
        self.assertEqual(calls[:3], ["credential", "restart", "listener"])


    async def test_app_startup_prints_bootstrap_password_path(self):
        bootstrap_path = Path("/state/admin-bootstrap-password")
        result = auth_service.BootstrapCredentialResult(
            rotated=True,
            source="generated",
            bootstrap_path=bootstrap_path,
        )
        with patch.object(main_app, "ensure_secure_admin_credential", return_value=result), \
                patch.object(main_app, "mark_update_started_after_restart"), \
                patch.object(main_app, "start_state_event_listener"), \
                patch.object(main_app, "BACKGROUND_TASKS_ENABLED", False), \
                patch.object(main_app, "append_business_log"), \
                patch("builtins.print") as print_mock:
            await main_app.resume_archive_queue()

        self.assertTrue(
            any(str(bootstrap_path) in " ".join(str(part) for part in call.args) for call in print_mock.call_args_list)
        )

if __name__ == "__main__":
    unittest.main()
