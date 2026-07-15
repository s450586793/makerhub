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
            patch.object(account_health, "publish_state_event"),
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

    def test_operational_status_prefers_three_mf_gate_over_account_probe(self):
        payload = account_health.operational_status_payload(
            "cn",
            {"status": "ok", "three_mf_gate": "cookie_invalid"},
        )

        self.assertEqual(
            payload,
            {
                "state": "cookie_invalid",
                "label": "需要重新登录",
                "tone": "danger",
                "message": "国内站 3MF 下载需要重新登录。",
                "action": "login",
            },
        )

    def test_operational_status_reports_updated_cookie_as_checking(self):
        payload = account_health.operational_status_payload(
            "cn",
            {
                "status": "unknown",
                "three_mf_gate": "unknown",
                "three_mf_reason": "cookie_updated",
                "three_mf_detail": "登录态已更新，正在检测 3MF 下载权限。",
            },
        )

        self.assertEqual(
            payload,
            {
                "state": "checking",
                "label": "检测中",
                "tone": "warning",
                "message": "登录态已更新，正在检测 3MF 下载权限。",
                "action": "none",
            },
        )

    def test_operational_status_maps_recovery_actions(self):
        cases = [
            ("verification_required", "需要浏览器确认", "warning", "browser"),
            ("daily_limit", "今日下载受限", "warning", "none"),
            ("network_error", "状态待确认", "warning", "test"),
            ("unknown", "状态待确认", "neutral", "test"),
            ("ok", "可归档", "ok", "none"),
        ]

        for status, label, tone, action in cases:
            with self.subTest(status=status):
                payload = account_health.operational_status_payload(
                    "global",
                    {"status": status, "three_mf_gate": "open"},
                )

                self.assertEqual(payload["state"], status)
                self.assertEqual(payload["label"], label)
                self.assertEqual(payload["tone"], tone)
                self.assertEqual(payload["action"], action)

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
        self.assertEqual(card["status"], "可归档")
        self.assertEqual(card["tone"], "ok")
        self.assertEqual(card["action_label"], "打开官网")
        self.assertEqual(card["url"], "https://makerworld.com.cn")
        self.assertNotIn("actions", card)

    def test_snapshot_to_source_card_keeps_verification_status_without_manual_actions(self):
        card = account_health.snapshot_to_source_card(
            "global",
            {
                "platform": "global",
                "status": "verification_required",
                "reason": "scheduled_cookie_check",
                "source": "scheduled_cookie_check",
                "detail": "国际站需要完成 Cloudflare 验证。",
                "updated_at": "2026-06-28T15:30:00+08:00",
            },
        )

        self.assertEqual(card["state"], "verification_required")
        self.assertEqual(card["status"], "需要浏览器确认")
        self.assertEqual(card["detail"], "国际站需要在浏览器完成验证后继续归档。")
        self.assertEqual(card["action_label"], "打开官网")
        self.assertEqual(card["url"], "https://makerworld.com")
        self.assertNotIn("actions", card)

    def test_snapshot_to_source_card_uses_planned_status_copy(self):
        cases = [
            ("daily_limit", "今日下载受限", "warning"),
            ("cookie_invalid", "需要重新登录", "danger"),
            ("network_error", "状态待确认", "warning"),
            ("unknown", "状态待确认", "neutral"),
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

    def test_update_three_mf_gate_preserves_account_status_and_updates_card(self):
        with patch.object(account_health, "china_now_iso", return_value="2026-06-23T16:40:00+08:00"):
            snapshot = account_health.update_three_mf_gate(
                "cn",
                gate="cookie_invalid",
                reason="cf_clearance_rejected",
                detail="国内站网页验证失效，请重新验证并更新 Cookie。",
                model_url="https://makerworld.com.cn/zh/models/1461337",
                model_id="1461337",
                instance_id="profile-1",
            )

        self.assertEqual(snapshot["status"], "ok")
        self.assertEqual(snapshot["three_mf_gate"], "cookie_invalid")
        self.assertEqual(snapshot["three_mf_reason"], "cf_clearance_rejected")
        self.assertEqual(snapshot["detail"], "国内站网页验证失效，请重新验证并更新 Cookie。")

        card = account_health.snapshot_to_source_card("cn", snapshot)
        self.assertEqual(card["state"], "cookie_invalid")
        self.assertEqual(card["status"], "需要重新登录")
        self.assertEqual(card["three_mf_gate"], "cookie_invalid")
        self.assertEqual(card["account_status"], "ok")

    def test_mark_account_ok_reopens_three_mf_gate_for_platform(self):
        account_health.update_three_mf_gate("global", gate="verification_required", reason="manual")

        snapshot = account_health.mark_account_ok("global", source="manual_verification")

        self.assertEqual(snapshot["status"], "ok")
        self.assertEqual(snapshot["three_mf_gate"], "open")
        self.assertEqual(snapshot["three_mf_reason"], "")

    def test_open_three_mf_gate_preserves_account_status(self):
        account_health.update_account_health(
            "cn",
            status="network_error",
            reason="probe_failed",
            source="web_probe",
            detail="账号页暂时不可达。",
        )
        account_health.update_three_mf_gate(
            "cn",
            gate="daily_limit",
            reason="download_limited",
            detail="已达到 MakerWorld 每日下载上限。",
        )

        snapshot = account_health.open_three_mf_gate(
            "cn",
            source="three_mf_limit_guard",
            detail="每日上限已恢复。",
        )

        self.assertEqual(snapshot["status"], "network_error")
        self.assertEqual(snapshot["reason"], "probe_failed")
        self.assertEqual(snapshot["three_mf_gate"], "open")
        self.assertEqual(snapshot["three_mf_reason"], "")
        self.assertEqual(snapshot["three_mf_detail"], "")

    def test_update_account_health_preserves_existing_three_mf_gate(self):
        account_health.update_three_mf_gate(
            "cn",
            gate="cookie_invalid",
            reason="cf_clearance_rejected",
            detail="国内站网页验证失效，请重新验证并更新 Cookie。",
        )

        snapshot = account_health.update_account_health(
            "cn",
            status="ok",
            source="web_probe",
            detail="账号页面可访问。",
        )

        self.assertEqual(snapshot["status"], "ok")
        self.assertEqual(snapshot["detail"], "账号页面可访问。")
        self.assertEqual(snapshot["three_mf_gate"], "cookie_invalid")
        self.assertEqual(snapshot["three_mf_reason"], "cf_clearance_rejected")


if __name__ == "__main__":
    unittest.main()
