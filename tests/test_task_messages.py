import unittest

from app.services import task_messages


class TaskMessagesTest(unittest.TestCase):
    def test_sanitize_message_text_hides_html_verification_page(self):
        message = task_messages.sanitize_message_text(
            "<html><body>cf-browser-verification</body></html>",
            fallback="fallback",
        )

        self.assertIn("风控校验页", message)
        self.assertNotIn("<html", message)

    def test_normalize_source_refresh_item_rewrites_legacy_remote_refresh_text(self):
        item = task_messages.normalize_source_refresh_item(
            {
                "id": "remote-1",
                "message": "远端刷新完成。",
            }
        )

        self.assertEqual(item["message"], "源端刷新完成。")

    def test_normalize_task_item_accepts_legacy_fields_and_sanitizes_message(self):
        item = task_messages.normalize_task_item(
            {
                "task_id": "task-1",
                "url": "https://example.test/model",
                "percent_complete": 42,
                "detail": "正在\n处理",
                "created_at": "2026-06-01T12:00:00+08:00",
                "meta": {"source": "cn"},
            },
            "queued",
        )

        self.assertEqual(item["id"], "task-1")
        self.assertEqual(item["title"], "https://example.test/model")
        self.assertEqual(item["progress"], 42)
        self.assertEqual(item["message"], "正在 处理")
        self.assertEqual(item["updated_at"], "2026-06-01T12:00:00+08:00")
        self.assertEqual(item["meta"]["source"], "cn")

    def test_organizer_event_message_uses_defaults_and_sanitizes_errors(self):
        self.assertEqual(
            task_messages.organizer_event_message({"event": "organized"}, "success"),
            "本地 3MF 已整理入库。",
        )
        self.assertEqual(
            task_messages.organizer_event_message({"event": "organize_failed"}, "failed"),
            "本地 3MF 整理失败。",
        )
        self.assertEqual(
            task_messages.organizer_event_message({"error": "<html>login</html>"}, "failed"),
            "返回了 HTML 页面，通常是 Cookie 失效、代理错误或站点风控页。",
        )

    def test_metadata_only_missing_3mf_placeholder_detection(self):
        self.assertTrue(
            task_messages.is_metadata_only_missing_3mf_placeholder(
                {"message": "信息补全任务会整理评论回复，不下载 3MF。"}
            )
        )
        self.assertFalse(task_messages.is_metadata_only_missing_3mf_placeholder({"message": "缺失 3MF"}))


if __name__ == "__main__":
    unittest.main()
