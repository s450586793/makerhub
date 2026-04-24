import unittest

from app.services import source_health


class SourceHealthCardsTest(unittest.TestCase):
    def setUp(self):
        self.original_executor = source_health.ThreadPoolExecutor
        source_health.ThreadPoolExecutor = InlineExecutor

    def tearDown(self):
        source_health.ThreadPoolExecutor = self.original_executor

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
        self.assertEqual(card_map["global"]["state"], "ok")

    def test_missing_3mf_limit_overrides_probe_verification(self):
        original_probe = source_health._probe_platform_status
        source_health._probe_platform_status = lambda platform, *_args, **_kwargs: {
            "platform": platform,
            "state": "verification_required",
            "status": "需要验证",
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
                        "message": "国区下载 3MF 时触发站点验证；请先在浏览器完成验证，再更新国内站 Cookie / token。",
                        "model_url": "https://makerworld.com.cn/zh/models/789",
                    }
                ],
            )
        finally:
            source_health._probe_platform_status = original_probe

        card_map = {item["key"]: item for item in cards}
        self.assertEqual(card_map["cn"]["state"], "verification_required")
        self.assertEqual(card_map["cn"]["status"], "需要验证")

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
