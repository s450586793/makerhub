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

        self.assertEqual(set(payload.keys()), {"cn", "global"})
        self.assertEqual(payload["cn"]["status"], "unknown")
        self.assertEqual(payload["cn"]["source"], "system")
        self.assertEqual(payload["cn"]["model_id"], "")
        self.assertEqual(payload["cn"]["instance_id"], "")
        self.assertEqual(payload["cn"]["updated_at"], "")
        self.assertEqual(payload["global"]["status"], "unknown")
        self.assertEqual(payload["global"]["source"], "system")
        self.assertEqual(payload["global"]["model_id"], "")
        self.assertEqual(payload["global"]["instance_id"], "")
        self.assertEqual(payload["global"]["updated_at"], "")

    def test_update_account_health_normalizes_platform_and_status_alias(self):
        snapshot = account_health.update_account_health(
            "mw_global",
            status="cloudflare",
            reason="captcha",
            source="probe",
            detail="需要 Cloudflare 验证。",
            model_url="https://makerworld.com/zh/models/123",
            model_id="model-123",
            instance_id="instance-1",
            updated_at="2026-06-11T10:00:00+08:00",
        )

        self.assertEqual(snapshot["platform"], "global")
        self.assertEqual(snapshot["status"], "verification_required")
        self.assertEqual(snapshot["reason"], "captcha")
        self.assertEqual(snapshot["source"], "probe")
        self.assertEqual(snapshot["detail"], "需要 Cloudflare 验证。")
        self.assertEqual(snapshot["model_url"], "https://makerworld.com/zh/models/123")
        self.assertEqual(snapshot["model_id"], "model-123")
        self.assertEqual(snapshot["instance_id"], "instance-1")
        self.assertEqual(snapshot["updated_at"], "2026-06-11T10:00:00+08:00")
        self.assertEqual(account_health.get_account_health("global"), snapshot)

    def test_update_account_health_keeps_reason_for_unknown_status(self):
        snapshot = account_health.update_account_health(
            "cn",
            status="weird_state",
            reason="upstream changed",
            source="system",
        )

        self.assertEqual(snapshot["status"], "unknown")
        self.assertEqual(snapshot["reason"], "upstream changed")

    def test_update_account_health_fills_updated_at_when_missing(self):
        with patch.object(account_health, "china_now_iso", return_value="2026-06-12T09:30:00+08:00"):
            snapshot = account_health.update_account_health(
                "cn",
                status="network_error",
                reason="timeout",
                source="probe",
                detail="请求超时",
                model_id="model-9",
                instance_id="inst-9",
            )

        self.assertEqual(snapshot["updated_at"], "2026-06-12T09:30:00+08:00")
        self.assertEqual(snapshot["model_id"], "model-9")
        self.assertEqual(snapshot["instance_id"], "inst-9")

    def test_mark_account_ok_supports_model_fields(self):
        snapshot = account_health.mark_account_ok(
            "global",
            source="manual",
            detail="已恢复",
            model_url="https://makerworld.com/models/ok",
            model_id="model-ok",
            instance_id="inst-ok",
            updated_at="2026-06-12T10:00:00+08:00",
        )

        self.assertEqual(snapshot["status"], "ok")
        self.assertEqual(snapshot["source"], "manual")
        self.assertEqual(snapshot["detail"], "已恢复")
        self.assertEqual(snapshot["model_url"], "https://makerworld.com/models/ok")
        self.assertEqual(snapshot["model_id"], "model-ok")
        self.assertEqual(snapshot["instance_id"], "inst-ok")
        self.assertEqual(snapshot["updated_at"], "2026-06-12T10:00:00+08:00")

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

    def test_snapshot_to_source_card_uses_planned_status_copy(self):
        cases = [
            ("daily_limit", "到达每日上限", "warning"),
            ("cookie_invalid", "Cookie 异常", "danger"),
            ("network_error", "网络异常", "warning"),
            ("unknown", "未检测", "neutral"),
        ]

        for status, label, tone in cases:
            with self.subTest(status=status):
                card = account_health.snapshot_to_source_card(
                    "global",
                    {
                        "platform": "global",
                        "status": status,
                        "detail": "detail",
                        "source": "probe",
                        "updated_at": "2026-06-11T10:00:00+08:00",
                    },
                )

                self.assertEqual(card["state"], status)
                self.assertEqual(card["status"], label)
                self.assertEqual(card["tone"], tone)
                self.assertEqual(card["url"], "https://makerworld.com")


if __name__ == "__main__":
    unittest.main()
