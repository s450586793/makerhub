import unittest

from app.services import source_health


class SourceHealthCardsTest(unittest.TestCase):
    def setUp(self):
        self.original_executor = source_health.ThreadPoolExecutor
        self.original_limit_guard_for_platform = source_health._limit_guard_for_platform
        source_health.ThreadPoolExecutor = InlineExecutor

    def tearDown(self):
        source_health.ThreadPoolExecutor = self.original_executor
        source_health._limit_guard_for_platform = self.original_limit_guard_for_platform

    def test_missing_3mf_verification_overrides_probe_ok(self):
        original_probe = source_health._probe_platform_status
        source_health._probe_platform_status = lambda platform, *_args, **_kwargs: {
            "platform": platform,
            "state": "ok",
            "status": "连接正常",
            "detail": "",
        }

        class Config:
            cookies = []
            proxy = None

        try:
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
        finally:
            source_health._probe_platform_status = original_probe

        card_map = {item["key"]: item for item in cards}
        self.assertEqual(card_map["cn"]["state"], "verification_required")
        self.assertEqual(card_map["cn"]["status"], "需要验证")
        self.assertEqual(card_map["cn"]["detail"], "MakerWorld 需要验证，前往官网任意下载一个模型。")
        self.assertEqual(card_map["global"]["state"], "ok")

    def test_missing_3mf_limit_overrides_probe_verification(self):
        original_probe = source_health._probe_platform_status
        source_health._probe_platform_status = lambda platform, *_args, **_kwargs: {
            "platform": platform,
            "state": "verification_required",
            "status": "需要验证",
            "detail": "",
        }
        source_health._limit_guard_for_platform = lambda platform: {
            "active": True,
            "limited_until": "2026-04-27T00:00:00+08:00",
            "message": "国区返回了每日下载上限，今日暂停自动重试。",
            "model_url": "https://makerworld.com.cn/zh/models/456",
        } if platform == "cn" else {}

        class Config:
            cookies = []
            proxy = None

        try:
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
        finally:
            source_health._probe_platform_status = original_probe

        card_map = {item["key"]: item for item in cards}
        self.assertEqual(card_map["cn"]["state"], "download_limited")
        self.assertEqual(card_map["cn"]["status"], "到达每日上限")
        self.assertIn("2026-04-27 00:00", card_map["cn"]["detail"])

    def test_stale_missing_3mf_limit_message_uses_current_guard(self):
        original_probe = source_health._probe_platform_status
        source_health._probe_platform_status = lambda platform, *_args, **_kwargs: {
            "platform": platform,
            "state": "ok",
            "status": "连接正常",
            "detail": "",
        }
        source_health._limit_guard_for_platform = lambda platform: {
            "active": True,
            "limited_until": "2026-04-27T00:00:00+08:00",
            "message": "国区返回了每日下载上限，今日暂停自动重试。",
            "model_url": "https://makerworld.com.cn/zh/models/456",
        } if platform == "cn" else {}

        class Config:
            cookies = []
            proxy = None

        try:
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
        finally:
            source_health._probe_platform_status = original_probe

        card_map = {item["key"]: item for item in cards}
        self.assertEqual(card_map["cn"]["state"], "download_limited")
        self.assertIn("2026-04-27 00:00", card_map["cn"]["detail"])
        self.assertNotIn("2026-04-26 00:00", card_map["cn"]["detail"])

    def test_stale_missing_3mf_limit_without_guard_does_not_override_probe_ok(self):
        original_probe = source_health._probe_platform_status
        source_health._probe_platform_status = lambda platform, *_args, **_kwargs: {
            "platform": platform,
            "state": "ok",
            "status": "连接正常",
            "detail": "",
        }
        source_health._limit_guard_for_platform = lambda _platform: {}

        class Config:
            cookies = []
            proxy = None

        try:
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
        finally:
            source_health._probe_platform_status = original_probe

        card_map = {item["key"]: item for item in cards}
        self.assertEqual(card_map["cn"]["state"], "ok")
        self.assertEqual(card_map["cn"]["status"], "连接正常")

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

    def test_missing_3mf_message_only_verification_overrides_probe_ok(self):
        original_probe = source_health._probe_platform_status
        source_health._probe_platform_status = lambda platform, *_args, **_kwargs: {
            "platform": platform,
            "state": "ok",
            "status": "连接正常",
            "detail": "",
        }

        class Config:
            cookies = []
            proxy = None

        try:
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
        finally:
            source_health._probe_platform_status = original_probe

        card_map = {item["key"]: item for item in cards}
        self.assertEqual(card_map["cn"]["state"], "verification_required")
        self.assertEqual(card_map["cn"]["status"], "需要验证")

    def test_retrying_missing_3mf_does_not_override_probe_ok(self):
        original_probe = source_health._probe_platform_status
        source_health._probe_platform_status = lambda platform, *_args, **_kwargs: {
            "platform": platform,
            "state": "ok",
            "status": "连接正常",
            "detail": "",
        }

        class Config:
            cookies = []
            proxy = None

        try:
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
        finally:
            source_health._probe_platform_status = original_probe

        card_map = {item["key"]: item for item in cards}
        self.assertEqual(card_map["cn"]["state"], "ok")
        self.assertEqual(card_map["cn"]["status"], "连接正常")

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
