import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.core.store import JsonStore
from app.schemas.models import CookiePair
from app.services import account_cookie_maintenance


class AccountCookieMaintenanceTest(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.store = JsonStore(Path(self.tmpdir.name) / "config.json")
        self.state = {}
        self.db_patches = [
            patch.object(
                account_cookie_maintenance,
                "load_database_json_state",
                side_effect=lambda key, default: dict(self.state.get(key) or default),
            ),
            patch.object(
                account_cookie_maintenance,
                "save_database_json_state",
                side_effect=lambda key, value: self.state.__setitem__(key, value) or value,
            ),
        ]
        for item in self.db_patches:
            item.start()

    def tearDown(self):
        for item in reversed(self.db_patches):
            item.stop()
        self.tmpdir.cleanup()

    def test_due_cookie_check_marks_cloudflare_verification_and_closes_gate(self):
        config = self.store.load()
        config.cookies = [CookiePair(platform="global", cookie="token=old", username="ace@example.com")]
        self.store.save(config)
        checked = []
        gate_updates = []

        def fake_metadata(**kwargs):
            checked.append(kwargs)
            return {
                "platform": "global",
                "username": "ace@example.com",
                "status": "verification_required",
                "message": "MakerWorld 需要验证，前往官网任意下载一个模型。",
                "last_tested_at": "2026-06-28T15:00:00+08:00",
                "updated_at": "2026-06-28T15:00:00+08:00",
            }

        with patch.object(account_cookie_maintenance, "china_now_iso", return_value="2026-06-28T15:00:00+08:00"), \
                patch.object(account_cookie_maintenance, "online_account_metadata_from_cookie", side_effect=fake_metadata), \
                patch.object(account_cookie_maintenance, "update_account_health") as update_health_mock, \
                patch.object(account_cookie_maintenance, "update_three_mf_gate", side_effect=lambda *args, **kwargs: gate_updates.append((args, kwargs)) or {"status": "verification_required"}), \
                patch.object(account_cookie_maintenance, "append_business_log"):
            result = account_cookie_maintenance.run_account_cookie_maintenance_once(
                store=self.store,
                interval_hours=12,
            )

        self.assertEqual(result["checked"], 1)
        self.assertEqual(result["failed"], 1)
        self.assertEqual(checked[0]["platform"], "global")
        self.assertEqual(self.store.load().cookies[0].status, "verification_required")
        self.assertEqual(self.store.load().cookies[0].message, "MakerWorld 需要验证，前往官网任意下载一个模型。")
        update_health_mock.assert_called_once_with(
            "global",
            status="verification_required",
            reason="scheduled_cookie_check",
            source="scheduled_cookie_check",
            detail="MakerWorld 需要验证，前往官网任意下载一个模型。",
            updated_at="2026-06-28T15:00:00+08:00",
        )
        self.assertEqual(gate_updates[0][0][0], "global")
        self.assertEqual(gate_updates[0][1]["gate"], "verification_required")
        self.assertEqual(gate_updates[0][1]["reason"], "scheduled_cookie_check")
        self.assertEqual(self.state["account_cookie_maintenance"]["last_run_at"], "2026-06-28T15:00:00+08:00")

    def test_skips_when_interval_has_not_elapsed(self):
        self.state["account_cookie_maintenance"] = {"last_run_at": "2026-06-28T08:00:00+08:00"}
        config = self.store.load()
        config.cookies = [CookiePair(platform="cn", cookie="token=old")]
        self.store.save(config)

        with patch.object(account_cookie_maintenance, "china_now_iso", return_value="2026-06-28T15:00:00+08:00"), \
                patch.object(account_cookie_maintenance, "online_account_metadata_from_cookie") as metadata_mock:
            result = account_cookie_maintenance.run_account_cookie_maintenance_once(
                store=self.store,
                interval_hours=12,
            )

        self.assertEqual(result["checked"], 0)
        self.assertEqual(result["skipped_reason"], "interval")
        metadata_mock.assert_not_called()

    def test_successful_check_marks_account_ok_without_reopening_closed_gate(self):
        config = self.store.load()
        config.cookies = [
            CookiePair(
                platform="cn",
                cookie="token=ok",
                username="13800138000",
                display_name="艾斯",
                account_id="2024907479",
                handle="s450586793",
                avatar_url="https://example.com/avatar.jpg",
            )
        ]
        self.store.save(config)

        with patch.object(account_cookie_maintenance, "china_now_iso", return_value="2026-06-28T15:00:00+08:00"), \
                patch.object(
                    account_cookie_maintenance,
                    "online_account_metadata_from_cookie",
                    return_value={
                        "platform": "cn",
                        "username": "13800138000",
                        "status": "ok",
                        "message": "国内账号可用，Cookie 已保存。",
                        "last_tested_at": "2026-06-28T15:00:00+08:00",
                        "updated_at": "2026-06-28T15:00:00+08:00",
                    },
                ), \
                patch.object(account_cookie_maintenance, "update_account_health") as update_health_mock, \
                patch.object(account_cookie_maintenance, "update_three_mf_gate") as update_gate_mock, \
                patch.object(account_cookie_maintenance, "append_business_log"):
            result = account_cookie_maintenance.run_account_cookie_maintenance_once(
                store=self.store,
                interval_hours=12,
            )

        self.assertEqual(result["checked"], 1)
        self.assertEqual(result["ok"], 1)
        saved_cookie = self.store.load().cookies[0]
        self.assertEqual(saved_cookie.status, "ok")
        self.assertEqual(saved_cookie.display_name, "艾斯")
        self.assertEqual(saved_cookie.account_id, "2024907479")
        self.assertEqual(saved_cookie.handle, "s450586793")
        self.assertEqual(saved_cookie.avatar_url, "https://example.com/avatar.jpg")
        update_health_mock.assert_called_once_with(
            "cn",
            status="ok",
            reason="scheduled_cookie_check",
            source="scheduled_cookie_check",
            detail="国内账号可用，Cookie 已保存。",
            updated_at="2026-06-28T15:00:00+08:00",
        )
        update_gate_mock.assert_not_called()


if __name__ == "__main__":
    unittest.main()
