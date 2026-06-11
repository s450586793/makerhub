import unittest
from unittest.mock import patch

from app.services import account_health


class AccountHealthServiceTest(unittest.TestCase):
    def setUp(self):
        self.state = {}
        self.db_patches = [
            patch.object(
                account_health,
                "load_database_json_state",
                side_effect=lambda key, default: dict(self.state.get(key) or default),
            ),
            patch.object(
                account_health,
                "save_database_json_state",
                side_effect=lambda key, value: self.state.__setitem__(key, value) or value,
            ),
        ]
        for item in self.db_patches:
            item.start()

    def tearDown(self):
        for item in reversed(self.db_patches):
            item.stop()

    def test_account_health_platforms_constant(self):
        self.assertEqual(account_health.ACCOUNT_HEALTH_PLATFORMS, ("cn", "global"))

    def test_load_account_health_returns_default_platforms(self):
        payload = account_health.load_account_health()

        self.assertEqual(set(payload["platforms"].keys()), {"cn", "global"})
        self.assertEqual(payload["platforms"]["cn"]["status"], "unknown")
        self.assertEqual(payload["platforms"]["cn"]["source"], "system")
        self.assertEqual(payload["platforms"]["global"]["status"], "unknown")
        self.assertEqual(payload["platforms"]["global"]["source"], "system")

    def test_update_account_health_normalizes_platform_and_status_alias(self):
        payload = account_health.update_account_health(
            "mw_global",
            status="cloudflare",
            reason="captcha",
            source="probe",
            detail="需要 Cloudflare 验证。",
            model_url="https://makerworld.com/zh/models/123",
            updated_at="2026-06-11T10:00:00+08:00",
        )

        snapshot = payload["platforms"]["global"]
        self.assertEqual(snapshot["platform"], "global")
        self.assertEqual(snapshot["status"], "verification_required")
        self.assertEqual(snapshot["reason"], "captcha")
        self.assertEqual(snapshot["source"], "probe")
        self.assertEqual(snapshot["detail"], "需要 Cloudflare 验证。")
        self.assertEqual(snapshot["model_url"], "https://makerworld.com/zh/models/123")
        self.assertEqual(snapshot["updated_at"], "2026-06-11T10:00:00+08:00")

    def test_update_account_health_keeps_reason_for_unknown_status(self):
        payload = account_health.update_account_health(
            "cn",
            status="weird_state",
            reason="upstream changed",
            source="system",
        )

        snapshot = payload["platforms"]["cn"]
        self.assertEqual(snapshot["status"], "unknown")
        self.assertEqual(snapshot["reason"], "upstream changed")

    def test_snapshot_to_source_card_returns_ok_card(self):
        card = account_health.snapshot_to_source_card(
            "cn",
            {
                "platform": "cn",
                "status": "ok",
                "source": "probe",
                "updated_at": "2026-06-11T10:00:00+08:00",
            },
        )

        self.assertEqual(card["state"], "ok")
        self.assertEqual(card["status"], "正常")
        self.assertEqual(card["tone"], "ok")
        self.assertEqual(card["action_label"], "打开官网")
        self.assertEqual(card["url"], "https://makerworld.com.cn")

    def test_snapshot_to_source_card_returns_verification_card(self):
        card = account_health.snapshot_to_source_card(
            "global",
            {
                "platform": "global",
                "status": "verification_required",
                "detail": "需要先完成网页验证。",
                "source": "probe",
                "updated_at": "2026-06-11T10:00:00+08:00",
            },
        )

        self.assertEqual(card["state"], "verification_required")
        self.assertEqual(card["status"], "需要验证")
        self.assertEqual(card["tone"], "danger")
        self.assertEqual(card["detail"], "需要先完成网页验证。")
        self.assertEqual(card["url"], "https://makerworld.com")


if __name__ == "__main__":
    unittest.main()
