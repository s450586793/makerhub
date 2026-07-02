import unittest
from types import SimpleNamespace
from unittest.mock import patch

from app.services import source_health


class SourceHealthCardsTest(unittest.TestCase):
    def setUp(self):
        self.original_executor = source_health.ThreadPoolExecutor
        self.original_limit_guard_for_platform = source_health._limit_guard_for_platform
        self.original_load_account_health = source_health.load_account_health
        source_health.ThreadPoolExecutor = InlineExecutor
        self.account_health_snapshots = self._snapshot_payload()
        source_health.load_account_health = lambda: {
            platform: dict(snapshot)
            for platform, snapshot in self.account_health_snapshots.items()
        }

    def tearDown(self):
        source_health.ThreadPoolExecutor = self.original_executor
        source_health._limit_guard_for_platform = self.original_limit_guard_for_platform
        source_health.load_account_health = self.original_load_account_health
        source_health.SOURCE_HEALTH_CACHE.clear()
        source_health.SOURCE_HEALTH_REFRESHING_KEYS.clear()

    def _snapshot_payload(self, cn=None, global_=None):
        def build(platform, overrides=None):
            payload = {
                "platform": platform,
                "status": "unknown",
                "reason": "",
                "source": "system",
                "detail": "",
                "model_url": "",
                "model_id": "",
                "instance_id": "",
                "updated_at": "",
            }
            payload.update(overrides or {})
            return payload

        return {
            "cn": build("cn", cn),
            "global": build("global", global_),
        }

    def _set_account_health(self, cn=None, global_=None):
        self.account_health_snapshots = self._snapshot_payload(cn, global_)

    def test_homepage_source_cards_use_account_health_snapshot(self):
        self._set_account_health(
            cn={
                "status": "verification_required",
                "source": "probe",
                "detail": "需要完成 MakerWorld 验证。",
                "updated_at": "2026-06-12T10:00:00+08:00",
            }
        )

        class Config:
            cookies = []
            proxy = None

        cards = source_health.build_source_health_cards(
            Config(),
            [
                {
                    "status": "verification_required",
                    "message": "",
                    "model_url": "https://makerworld.com.cn/zh/models/123",
                }
            ],
        )

        card_map = {item["key"]: item for item in cards}
        self.assertEqual(card_map["cn"]["state"], "verification_required")
        self.assertEqual(card_map["cn"]["status"], "需要验证")
        self.assertEqual(card_map["cn"]["detail"], "需要完成 MakerWorld 验证。")
        self.assertEqual(card_map["cn"]["source"], "probe")
        self.assertEqual(card_map["cn"].get("action_label"), "手动过 CF")
        self.assertEqual(card_map["cn"].get("url"), "https://makerworld.com.cn")
        self.assertEqual(card_map["cn"].get("actions"), [
            {
                "kind": "external",
                "label": "手动过 CF",
                "href": "https://makerworld.com.cn",
            },
            {
                "kind": "api",
                "label": "已验证",
                "endpoint": "/api/tasks/missing-3mf/verification-verified",
                "method": "POST",
                "body": {"platform": "cn"},
            },
        ])
        self.assertNotIn("route", card_map["cn"])
        self.assertEqual(card_map["global"]["state"], "unknown")
        self.assertEqual(card_map["global"]["status"], "未检测")
        self.assertEqual(card_map["cn"]["checks"], [])

    def test_missing_3mf_verification_does_not_override_account_health_ok_snapshot(self):
        self._set_account_health(
            cn={
                "status": "ok",
                "source": "probe",
                "detail": "账号状态正常。",
                "updated_at": "2026-06-12T10:10:00+08:00",
            }
        )

        class Config:
            cookies = []
            proxy = None

        cards = source_health.build_source_health_cards(
            Config(),
            [
                {
                    "status": "verification_required",
                    "message": "",
                    "model_url": "https://makerworld.com/zh/models/123",
                }
            ],
        )

        card_map = {item["key"]: item for item in cards}
        self.assertEqual(card_map["cn"]["state"], "ok")
        self.assertEqual(card_map["cn"]["status"], "正常")
        self.assertEqual(card_map["cn"]["detail"], "账号状态正常。")
        self.assertEqual(card_map["cn"].get("action_label"), "打开官网")
        self.assertEqual(card_map["cn"].get("url"), "https://makerworld.com.cn")
        self.assertNotIn("route", card_map["cn"])
        self.assertEqual(card_map["cn"]["checks"], [])
        self.assertEqual(card_map["global"]["state"], "unknown")
        self.assertEqual(card_map["global"]["status"], "未检测")

    def test_missing_3mf_verification_source_without_model_url_does_not_change_homepage_status(self):
        class Config:
            cookies = []
            proxy = None

        cards = source_health.build_source_health_cards(
            Config(),
            [
                {
                    "status": "CLOUDFLARE",
                    "source": "mw_global",
                    "message": "",
                }
            ],
        )

        card_map = {item["key"]: item for item in cards}
        self.assertEqual(card_map["global"]["state"], "unknown")
        self.assertEqual(card_map["global"]["status"], "未检测")
        self.assertEqual(card_map["global"]["detail"], "")
        self.assertEqual(card_map["global"]["checks"], [])

    def test_account_probe_state_is_ignored_when_snapshot_missing(self):
        class Config:
            cookies = []
            proxy = None

        with patch.object(
            source_health,
            "_probe_platform_status",
            side_effect=AssertionError("homepage source cards should not probe account status"),
        ):
            cards = source_health.build_source_health_cards(Config(), [])

        card_map = {item["key"]: item for item in cards}
        self.assertEqual(card_map["cn"]["state"], "unknown")
        self.assertEqual(card_map["cn"]["status"], "未检测")
        self.assertEqual(card_map["cn"].get("url"), "https://makerworld.com.cn")
        self.assertEqual(card_map["cn"].get("action_label"), "打开官网")
        self.assertNotIn("route", card_map["cn"])
        self.assertEqual(card_map["cn"]["checks"], [])

    def test_source_health_prefer_cached_returns_checking_without_probe_blocking(self):
        original_probe = source_health._probe_auth_endpoints
        original_async_refresh = source_health._async_refresh_source_health
        calls = []

        def fail_probe(*_args, **_kwargs):
            raise AssertionError("dashboard snapshot should not block on source probe")

        source_health._probe_auth_endpoints = fail_probe
        source_health._async_refresh_source_health = lambda *args, **_kwargs: calls.append(args)
        source_health._limit_guard_for_platform = lambda _platform: {}

        class Config:
            cookies = [
                SimpleNamespace(platform="cn", cookie="sid=cn"),
                SimpleNamespace(platform="global", cookie="sid=global"),
            ]
            proxy = None

        try:
            cards = source_health.build_source_health_cards(Config(), [], prefer_cached=True)
        finally:
            source_health._probe_auth_endpoints = original_probe
            source_health._async_refresh_source_health = original_async_refresh

        card_map = {item["key"]: item for item in cards}
        self.assertEqual(card_map["cn"]["state"], "unknown")
        self.assertEqual(card_map["cn"]["status"], "未检测")
        self.assertEqual(card_map["cn"]["tone"], "neutral")
        self.assertEqual(card_map["global"]["state"], "unknown")
        self.assertEqual(len(calls), 0)

    def test_source_health_keeps_account_ok_when_web_probe_fails(self):
        self._set_account_health(cn={"status": "ok", "detail": "账号状态正常。"})

        class Config:
            cookies = [
                SimpleNamespace(platform="cn", cookie="sid=cn"),
                SimpleNamespace(platform="global", cookie="sid=global"),
            ]
            proxy = None

        with patch.object(
            source_health,
            "_probe_platform_status",
            side_effect=AssertionError("homepage source cards should not probe account status"),
        ), patch.object(
            source_health,
            "_probe_platform_web_status",
            side_effect=AssertionError("homepage source cards should not probe web status"),
        ):
            cards = source_health.build_source_health_cards(Config(), [])

        card_map = {item["key"]: item for item in cards}
        self.assertEqual(card_map["cn"]["state"], "ok")
        self.assertEqual(card_map["cn"]["status"], "正常")
        self.assertEqual(card_map["cn"]["tone"], "ok")
        self.assertEqual(card_map["cn"]["detail"], "账号状态正常。")
        self.assertEqual(card_map["cn"].get("action_label"), "打开官网")
        self.assertEqual(card_map["cn"]["checks"], [])
        self.assertEqual(card_map["global"]["state"], "unknown")

    def test_source_health_keeps_account_ok_when_web_probe_reports_auth_required(self):
        self._set_account_health(global_={"status": "ok", "detail": "国际站账号正常。"})

        class Config:
            cookies = [
                SimpleNamespace(platform="cn", cookie="sid=cn"),
                SimpleNamespace(platform="global", cookie="sid=global"),
            ]
            proxy = None

        with patch.object(
            source_health,
            "_probe_platform_status",
            side_effect=AssertionError("homepage source cards should not probe account status"),
        ), patch.object(
            source_health,
            "_probe_platform_web_status",
            side_effect=AssertionError("homepage source cards should not probe web status"),
        ):
            cards = source_health.build_source_health_cards(Config(), [])

        card_map = {item["key"]: item for item in cards}
        self.assertEqual(card_map["global"]["state"], "ok")
        self.assertEqual(card_map["global"]["status"], "正常")
        self.assertEqual(card_map["global"]["tone"], "ok")
        self.assertEqual(card_map["global"]["detail"], "国际站账号正常。")
        self.assertEqual(card_map["global"]["checks"], [])

    def test_source_health_prefer_cached_web_probe_does_not_block(self):
        original_async_refresh = source_health._async_refresh_source_health
        calls = []
        source_health._async_refresh_source_health = lambda *args, **_kwargs: calls.append(args)
        source_health._limit_guard_for_platform = lambda _platform: {}

        class Config:
            cookies = [SimpleNamespace(platform="cn", cookie="sid=cn")]
            proxy = None

        try:
            cards = source_health.build_source_health_cards(Config(), [], prefer_cached=True)
        finally:
            source_health._async_refresh_source_health = original_async_refresh

        card_map = {item["key"]: item for item in cards}
        self.assertEqual(card_map["cn"]["state"], "unknown")
        self.assertEqual(card_map["cn"]["status"], "未检测")
        self.assertEqual(card_map["cn"]["checks"], [])
        self.assertEqual(len(calls), 0)

    def test_source_health_prefer_cached_uses_stale_web_verification_snapshot(self):
        original_async_refresh = source_health._async_refresh_source_health
        calls = []

        class Config:
            cookies = [SimpleNamespace(platform="cn", cookie="sid=cn")]
            proxy = None

        account_key = source_health._cache_key("account", "cn", "sid=cn", None)
        web_key = source_health._cache_key("web", "cn", "sid=cn", None)
        source_health.SOURCE_HEALTH_CACHE[account_key] = {
            "checked_at": source_health.time.time(),
            "payload": {
                "platform": "cn",
                "state": "ok",
                "status": "连接正常",
                "detail": "",
            },
        }
        source_health.SOURCE_HEALTH_CACHE[web_key] = {
            "checked_at": source_health.time.time() - source_health.SOURCE_HEALTH_CACHE_TTL_SECONDS - 1,
            "payload": {
                "platform": "cn",
                "state": "verification_required",
                "status": "需要验证",
                "detail": "MakerWorld 网页入口返回验证页。",
            },
        }
        source_health._async_refresh_source_health = lambda *args, **_kwargs: calls.append(args)
        source_health._limit_guard_for_platform = lambda _platform: {}

        try:
            cards = source_health.build_source_health_cards(Config(), [], prefer_cached=True)
        finally:
            source_health._async_refresh_source_health = original_async_refresh

        card_map = {item["key"]: item for item in cards}
        self.assertEqual(card_map["cn"]["state"], "unknown")
        self.assertEqual(card_map["cn"]["status"], "未检测")
        self.assertEqual(card_map["cn"]["checks"], [])
        self.assertEqual(len(calls), 0)

    def test_web_probe_treats_normal_html_as_ok(self):
        result = source_health._web_probe_payload_from_response(
            platform="cn",
            url="https://makerworld.com.cn/zh",
            status_code=200,
            text="<html><body><title>MakerWorld</title></body></html>",
            headers={"content-type": "text/html"},
            elapsed_ms=12.0,
            engine="unit",
        )

        self.assertEqual(result["state"], "ok")
        self.assertEqual(result["status"], "访问正常")

    def test_web_probe_detects_verification_html(self):
        result = source_health._web_probe_payload_from_response(
            platform="global",
            url="https://makerworld.com/zh",
            status_code=200,
            text="<html><body>cf-browser-verification</body></html>",
            headers={"content-type": "text/html"},
            elapsed_ms=12.0,
            engine="unit",
        )

        self.assertEqual(result["state"], "verification_required")
        self.assertEqual(result["status"], "需要验证")

    def test_source_health_prefer_cached_uses_stale_snapshot_and_refreshes_background(self):
        original_async_refresh = source_health._async_refresh_source_health
        calls = []

        class Config:
            cookies = [SimpleNamespace(platform="cn", cookie="sid=cn")]
            proxy = None

        cache_key = source_health._cache_key("account", "cn", "sid=cn", None)
        source_health.SOURCE_HEALTH_CACHE[cache_key] = {
            "checked_at": source_health.time.time() - source_health.SOURCE_HEALTH_CACHE_TTL_SECONDS - 1,
            "payload": {
                "platform": "cn",
                "state": "ok",
                "status": "连接正常",
                "detail": "",
            },
        }
        source_health._async_refresh_source_health = lambda *args, **_kwargs: calls.append(args)
        source_health._limit_guard_for_platform = lambda _platform: {}

        try:
            cards = source_health.build_source_health_cards(Config(), [], prefer_cached=True)
        finally:
            source_health._async_refresh_source_health = original_async_refresh

        card_map = {item["key"]: item for item in cards}
        self.assertEqual(card_map["cn"]["state"], "unknown")
        self.assertEqual(card_map["cn"]["status"], "未检测")
        self.assertEqual(card_map["cn"]["checks"], [])
        self.assertEqual(len(calls), 0)

    def test_html_probe_without_verification_markers_is_interface_limited(self):
        result = source_health._auth_probe_result_from_response(
            name="profile",
            url="https://api.example.test/profile",
            status_code=200,
            text="<html><body>not json</body></html>",
            headers={"content-type": "text/html"},
            elapsed_ms=12.0,
        )

        self.assertFalse(result["ok"])
        self.assertEqual(result["failure_kind"], "html_response")
        self.assertIn("认证探针返回了网页页面", result["error"])
        self.assertNotIn("Cookie 失效", result["error"])
        self.assertEqual(source_health._classify_auth_probe_result(result), "html_response")

    def test_html_probe_with_verification_markers_still_requires_verification(self):
        result = source_health._auth_probe_result_from_response(
            name="profile",
            url="https://api.example.test/profile",
            status_code=200,
            text="<html><body>cf-browser-verification</body></html>",
            headers={"content-type": "text/html"},
            elapsed_ms=12.0,
        )

        self.assertFalse(result["ok"])
        self.assertEqual(result["failure_kind"], "verification_required")
        self.assertIn("验证页面", result["error"])

    def test_auth_probe_uses_flaresolverr_without_session_get(self):
        class FailingSession:
            def close(self):
                return None

            def get(self, *_args, **_kwargs):
                raise AssertionError("source health auth probe must use FlareSolverr")

        calls = []

        def fake_flaresolverr(url, **kwargs):
            calls.append((url, kwargs))
            return '{"uid": 1, "name": "ok"}'

        with patch.object(source_health, "_make_session", return_value=FailingSession()), patch.object(
            source_health,
            "flaresolverr_get_text",
            side_effect=fake_flaresolverr,
        ):
            payload = source_health._probe_auth_endpoints("cn", "token=abc", None)

        self.assertTrue(payload["ok"])
        self.assertFalse(payload["used_proxy"])
        self.assertTrue(calls)
        self.assertIn("Cookie", calls[0][1]["headers"])

    def test_web_probe_uses_flaresolverr_without_session_get(self):
        class FailingSession:
            def close(self):
                return None

            def get(self, *_args, **_kwargs):
                raise AssertionError("source health web probe must use FlareSolverr")

        with patch.object(source_health, "_make_session", return_value=FailingSession()), patch.object(
            source_health,
            "flaresolverr_get_text",
            return_value="<html><script id=\"__NEXT_DATA__\"></script></html>",
        ):
            payload = source_health._probe_platform_web_page("cn", "token=abc", None)

        self.assertTrue(payload["ok"])
        self.assertEqual(payload["state"], "ok")

    def test_cookie_partial_success_message_avoids_probe_count_jargon(self):
        message = source_health._build_cookie_auth_message(
            "cn",
            {
                "success_count": 1,
                "target_count": 2,
                "results": [
                    {"target": "消息计数", "ok": True},
                    {"target": "个人偏好", "ok": False},
                ],
            },
        )

        self.assertIn("国内账号已保存", message)
        self.assertIn("部分账号信息暂时读取失败", message)
        self.assertNotIn("1/2", message)
        self.assertNotIn("接口可访问", message)
        self.assertNotIn("个人偏好", message)

    def test_html_response_message_does_not_ask_for_verification(self):
        message = source_health._build_cookie_auth_message(
            "cn",
            {
                "state": "html_response",
                "success_count": 0,
                "target_count": 1,
                "results": [
                    {"target": "消息计数", "ok": False, "failure_kind": "html_response"},
                ],
            },
        )

        self.assertIn("暂时无法读取账号信息", message)
        self.assertNotIn("需要验证", message)

    def test_cookie_probe_headers_include_bearer_token_from_cookie(self):
        headers = source_health._build_request_headers(
            "https://makerworld.com.cn",
            "token=access-token; refreshToken=refresh-token",
        )

        self.assertEqual(headers["Authorization"], "Bearer access-token")
        self.assertEqual(headers["token"], "access-token")
        self.assertEqual(headers["X-Token"], "access-token")
        self.assertEqual(headers["X-Access-Token"], "access-token")

    def test_missing_3mf_limit_overrides_probe_verification(self):
        self._set_account_health(cn={"status": "ok", "detail": "账号状态正常。"})
        source_health._limit_guard_for_platform = lambda platform: {
            "active": True,
            "limited_until": "2026-04-27T00:00:00+08:00",
            "message": "国区返回了每日下载上限，今日暂停自动重试。",
            "model_url": "https://makerworld.com.cn/zh/models/456",
        } if platform == "cn" else {}

        class Config:
            cookies = []
            proxy = None

        cards = source_health.build_source_health_cards(
            Config(),
            [
                {
                    "status": "download_limited",
                    "message": "",
                    "model_url": "https://makerworld.com.cn/zh/models/456",
                }
            ],
        )

        card_map = {item["key"]: item for item in cards}
        self.assertEqual(card_map["cn"]["state"], "ok")
        self.assertEqual(card_map["cn"]["status"], "正常")
        self.assertEqual(card_map["cn"]["detail"], "账号状态正常。")
        self.assertEqual(card_map["cn"]["checks"], [])

    def test_stale_missing_3mf_limit_message_uses_current_guard(self):
        source_health._limit_guard_for_platform = lambda platform: {
            "active": True,
            "limited_until": "2026-04-27T00:00:00+08:00",
            "message": "国区返回了每日下载上限，今日暂停自动重试。",
            "model_url": "https://makerworld.com.cn/zh/models/456",
        } if platform == "cn" else {}

        class Config:
            cookies = []
            proxy = None

        cards = source_health.build_source_health_cards(
            Config(),
            [
                {
                    "status": "download_limited",
                    "message": "国区返回了每日下载上限，今日暂停自动重试，自动重试暂停至 2026-04-26 00:00。",
                    "model_url": "https://makerworld.com.cn/zh/models/456",
                }
            ],
        )

        card_map = {item["key"]: item for item in cards}
        self.assertEqual(card_map["cn"]["state"], "unknown")
        self.assertEqual(card_map["cn"]["status"], "未检测")
        self.assertEqual(card_map["cn"]["detail"], "")
        self.assertEqual(card_map["cn"]["checks"], [])

    def test_stale_missing_3mf_limit_without_guard_does_not_override_probe_ok(self):
        source_health._limit_guard_for_platform = lambda _platform: {}

        class Config:
            cookies = []
            proxy = None

        cards = source_health.build_source_health_cards(
            Config(),
            [
                {
                    "status": "download_limited",
                    "message": "国区返回了每日下载上限，今日暂停自动重试，自动重试暂停至 2026-04-26 00:00。",
                    "model_url": "https://makerworld.com.cn/zh/models/456",
                }
            ],
        )

        card_map = {item["key"]: item for item in cards}
        self.assertEqual(card_map["cn"]["state"], "unknown")
        self.assertEqual(card_map["cn"]["status"], "未检测")

    def test_probe_limit_guard_includes_guard_message(self):
        source_health._limit_guard_for_platform = lambda platform: {
            "active": True,
            "limited_until": "2026-04-27T00:00:00+08:00",
            "message": "国区返回了每日下载上限，今日暂停自动重试。",
            "model_url": "https://makerworld.com.cn/zh/models/456",
        } if platform == "cn" else {}

        payload = source_health.probe_cookie_auth_status(
            "cn",
            "foo=bar",
            None,
            include_limit_guard=True,
            use_cache=False,
        )

        self.assertEqual(payload["state"], "download_limited")
        self.assertEqual(payload["status"], "到达每日上限")
        self.assertIn("2026-04-27 00:00", payload["detail"])

    def test_proxy_mapping_bypasses_cn_platform(self):
        proxy = SimpleNamespace(
            enabled=True,
            http_proxy="http://proxy.local:7890",
            https_proxy="http://proxy.local:7891",
        )

        self.assertEqual(source_health._build_proxy_mapping(proxy, platform="cn"), {})
        self.assertEqual(
            source_health._build_proxy_mapping(proxy, platform="global"),
            {"http": "http://proxy.local:7890", "https": "http://proxy.local:7891"},
        )

    def test_proxy_mapping_can_allow_cn_account_auth_proxy(self):
        proxy = SimpleNamespace(
            enabled=True,
            http_proxy="http://proxy.local:7890",
            https_proxy="http://proxy.local:7891",
        )

        self.assertEqual(
            source_health._build_proxy_mapping(proxy, platform="cn", allow_domestic_proxy=True),
            {"http": "http://proxy.local:7890", "https": "http://proxy.local:7891"},
        )

    def test_cookie_probe_cache_key_treats_cn_proxy_as_bypassed(self):
        proxy_a = SimpleNamespace(
            enabled=True,
            http_proxy="http://proxy-a.local:7890",
            https_proxy="http://proxy-a.local:7891",
        )
        proxy_b = SimpleNamespace(
            enabled=True,
            http_proxy="http://proxy-b.local:7890",
            https_proxy="http://proxy-b.local:7891",
        )

        self.assertEqual(
            source_health._cache_key("account", "cn", "foo=bar", proxy_a),
            source_health._cache_key("account", "cn", "foo=bar", proxy_b),
        )
        self.assertNotEqual(
            source_health._cache_key("account", "global", "foo=bar", proxy_a),
            source_health._cache_key("account", "global", "foo=bar", proxy_b),
        )

    def test_cookie_probe_cache_key_includes_cn_proxy_when_allowed(self):
        proxy_a = SimpleNamespace(
            enabled=True,
            http_proxy="http://proxy-a.local:7890",
            https_proxy="http://proxy-a.local:7891",
        )
        proxy_b = SimpleNamespace(
            enabled=True,
            http_proxy="http://proxy-b.local:7890",
            https_proxy="http://proxy-b.local:7891",
        )

        self.assertNotEqual(
            source_health._cache_key("account", "cn", "foo=bar", proxy_a, allow_domestic_proxy=True),
            source_health._cache_key("account", "cn", "foo=bar", proxy_b, allow_domestic_proxy=True),
        )

    def test_cookie_probe_session_ignores_env_proxy(self):
        session = source_health._make_session()
        try:
            self.assertFalse(session.trust_env)
        finally:
            session.close()

    def test_missing_3mf_message_only_verification_does_not_override_probe_ok(self):
        class Config:
            cookies = []
            proxy = None

        cards = source_health.build_source_health_cards(
            Config(),
            [
                {
                    "status": "missing",
                    "message": "MakerWorld 需要验证，前往官网任意下载一个模型。",
                    "model_url": "https://makerworld.com.cn/zh/models/789",
                }
            ],
        )

        card_map = {item["key"]: item for item in cards}
        self.assertEqual(card_map["cn"]["state"], "unknown")
        self.assertEqual(card_map["cn"]["status"], "未检测")
        self.assertEqual(card_map["cn"]["checks"], [])

    def test_retrying_missing_3mf_does_not_override_probe_ok(self):
        class Config:
            cookies = []
            proxy = None

        cards = source_health.build_source_health_cards(
            Config(),
            [
                {
                    "status": "queued",
                    "message": "MakerWorld 需要验证，前往官网任意下载一个模型。",
                    "model_url": "https://makerworld.com.cn/zh/models/789",
                }
            ],
        )

        card_map = {item["key"]: item for item in cards}
        self.assertEqual(card_map["cn"]["state"], "unknown")
        self.assertEqual(card_map["cn"]["status"], "未检测")
        self.assertEqual(card_map["cn"]["checks"], [])

    def test_probe_verification_becomes_partial_when_recent_refresh_mostly_succeeds(self):
        self._set_account_health(cn={"status": "verification_required", "detail": "需要完成 MakerWorld 验证。"})

        class Config:
            cookies = []
            proxy = None

        recent_items = [
            {
                "status": "success",
                "url": f"https://makerworld.com.cn/zh/models/{idx}",
            }
            for idx in range(8)
        ] + [
            {
                "status": "failed",
                "message": "页面被 Cloudflare 验证拦截，请更新 cookie（含 cf_clearance）后重试",
                "url": "https://makerworld.com.cn/zh/models/999",
            }
        ]

        cards = source_health.build_source_health_cards(
            Config(),
            [],
            remote_refresh_state={"recent_items": recent_items},
        )

        card_map = {item["key"]: item for item in cards}
        self.assertEqual(card_map["cn"]["state"], "verification_required")
        self.assertEqual(card_map["cn"]["status"], "需要验证")
        self.assertEqual(card_map["cn"]["tone"], "danger")
        self.assertEqual(card_map["cn"]["detail"], "需要完成 MakerWorld 验证。")
        self.assertEqual(card_map["cn"]["checks"], [])

    def test_missing_3mf_verification_stays_normal_when_probe_is_softened_by_refresh(self):
        self._set_account_health(cn={"status": "ok", "detail": "账号状态正常。"})

        class Config:
            cookies = []
            proxy = None

        recent_items = [
            {
                "status": "success",
                "url": f"https://makerworld.com.cn/zh/models/{idx}",
            }
            for idx in range(10)
        ]

        cards = source_health.build_source_health_cards(
            Config(),
            [
                {
                    "status": "verification_required",
                    "message": "",
                    "model_url": "https://makerworld.com.cn/zh/models/123",
                }
            ],
            remote_refresh_state={"recent_items": recent_items},
        )

        card_map = {item["key"]: item for item in cards}
        self.assertEqual(card_map["cn"]["state"], "ok")
        self.assertEqual(card_map["cn"]["status"], "正常")
        self.assertEqual(card_map["cn"]["tone"], "ok")
        self.assertEqual(card_map["cn"]["detail"], "账号状态正常。")
        self.assertEqual(card_map["cn"]["checks"], [])

    def test_account_health_ok_snapshot_ignores_verification_history_from_missing_3mf_and_refresh(self):
        self._set_account_health(
            cn={
                "status": "ok",
                "source": "archive_download",
                "detail": "账号状态正常。",
                "updated_at": "2026-06-12T10:20:00+08:00",
            }
        )

        class Config:
            cookies = []
            proxy = None

        cards = source_health.build_source_health_cards(
            Config(),
            [
                {
                    "status": "verification_required",
                    "message": "",
                    "model_url": "https://makerworld.com.cn/zh/models/123",
                }
            ],
            remote_refresh_state={
                "recent_items": [
                    {
                        "status": "failed",
                        "message": "页面被 Cloudflare 验证拦截，请更新 cookie（含 cf_clearance）后重试",
                        "url": "https://makerworld.com.cn/zh/models/456",
                    }
                ]
            },
        )

        card_map = {item["key"]: item for item in cards}
        self.assertEqual(card_map["cn"]["state"], "ok")
        self.assertEqual(card_map["cn"]["status"], "正常")
        self.assertEqual(card_map["cn"]["tone"], "ok")
        self.assertEqual(card_map["cn"]["detail"], "账号状态正常。")
        self.assertEqual(card_map["cn"]["source"], "archive_download")
        self.assertEqual(card_map["cn"]["updated_at"], "2026-06-12T10:20:00+08:00")
        self.assertEqual(card_map["cn"]["checks"], [])

class InlineExecutor:
    def __init__(self, *args, **kwargs):
        pass

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def map(self, func, iterable):
        return [func(item) for item in iterable]


if __name__ == "__main__":
    unittest.main()
