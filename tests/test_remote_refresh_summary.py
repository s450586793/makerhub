import unittest

from app.services import remote_refresh_summary


class RemoteRefreshSummaryTest(unittest.TestCase):
    def test_sanitize_message_hides_html_verification_page(self):
        message = remote_refresh_summary.sanitize_remote_refresh_message(
            "<!doctype html><html><script>cf_clearance</script></html>",
            fallback="fallback",
        )

        self.assertIn("风控校验页", message)
        self.assertNotIn("<html", message)

    def test_result_record_adds_metrics_labels_and_progress(self):
        record = remote_refresh_summary.remote_refresh_result_record(
            model_dir="remote/model-1",
            title="模型 1",
            url="https://makerworld.com.cn/model/1",
            status="success",
            message="完成\n刷新",
            metrics={"total_duration_ms": 12},
            change_labels=["评论 +1", "", "附件 +2"],
        )

        self.assertEqual(record["id"], "remote/model-1")
        self.assertEqual(record["progress"], 100)
        self.assertEqual(record["message"], "完成 刷新")
        self.assertEqual(record["meta"]["metrics"]["total_duration_ms"], 12)
        self.assertEqual(record["meta"]["change_summary"], "评论 +1，附件 +2")

    def test_batch_summary_counts_statuses_and_limits_recent_items(self):
        records = [
            remote_refresh_summary.remote_refresh_result_record(
                model_dir=f"m{index}",
                title=f"模型 {index}",
                url=f"https://makerworld.com.cn/model/{index}",
                status="failed" if index % 2 == 0 else "skipped",
                message=f"失败 {index}",
            )
            for index in range(60)
        ]

        summary = remote_refresh_summary.remote_refresh_batch_summary(records)

        self.assertEqual(summary["failed"], 30)
        self.assertEqual(summary["skipped"], 30)
        self.assertEqual(len(summary["recent_items"]), 50)
        self.assertEqual(summary["recent_items"][0]["id"], "m59")
        self.assertEqual(len(summary["failure_samples"]), 10)

    def test_success_and_scope_messages(self):
        self.assertEqual(
            remote_refresh_summary.build_success_message(["已检查，无远端变化"]),
            "源端刷新完成，已检查，未发现远端内容变化。",
        )
        self.assertEqual(
            remote_refresh_summary.build_success_message(["评论 +2", "附件 +1"]),
            "源端刷新完成：评论 +2，附件 +1。",
        )
        self.assertEqual(
            remote_refresh_summary.batch_scope_message(eligible_total=0, remaining_total=9),
            "当前没有可刷新的远端模型。",
        )
        self.assertEqual(
            remote_refresh_summary.batch_scope_message(eligible_total=3, remaining_total=-1),
            "当前可刷新 3 个模型，剩余 0 个待补跑。",
        )


if __name__ == "__main__":
    unittest.main()
