import unittest
from pathlib import Path

from app.services.archive_profile_backfill import _meta_needs_profile_backfill
from app.services.catalog import _normalize_comment_item, _normalize_comments
from app.services.legacy_archiver import COMMENT_SCHEMA_VERSION, PROFILE_DETAIL_SCHEMA_VERSION, _collect_comment_tree


class CommentRepliesTest(unittest.TestCase):
    def test_catalog_normalizes_wrapped_reply_lists(self):
        comment = {
            "commentId": "root-comment",
            "commentContent": "主评论",
            "commentTime": "2026-04-23 12:00:00",
            "replyCount": 1,
            "subCommentVOList": {
                "records": [
                    {
                        "commentId": "reply-comment",
                        "commentContent": "第一条回复",
                        "commentTime": "2026-04-23 12:05:00",
                        "replyToName": "楼主",
                    }
                ]
            },
        }

        normalized = _normalize_comment_item(comment, Path("."))

        self.assertIsNotNone(normalized)
        self.assertEqual(normalized["reply_count"], 1)
        self.assertEqual(len(normalized["replies"]), 1)
        self.assertEqual(normalized["replies"][0]["content"], "第一条回复")
        self.assertEqual(normalized["replies"][0]["reply_to"], "楼主")

    def test_archiver_collects_wrapped_reply_lists(self):
        comment = {
            "commentId": "root-comment",
            "commentContent": "主评论",
            "commentTime": "2026-04-23 12:00:00",
            "replyCount": 1,
            "commentReplyList": {
                "items": [
                    {
                        "commentId": "reply-comment",
                        "commentContent": "第一条回复",
                        "commentTime": "2026-04-23 12:05:00",
                    }
                ]
            },
        }

        normalized, is_new = _collect_comment_tree(comment, {})

        self.assertTrue(is_new)
        self.assertIsNotNone(normalized)
        self.assertEqual(normalized["replyCount"], 1)
        self.assertEqual(len(normalized["replies"]), 1)
        self.assertEqual(normalized["replies"][0]["content"], "第一条回复")

    def test_catalog_threads_flattened_replies_back_under_root_comment(self):
        meta = {
            "comments": [
                {
                    "commentId": "root-comment",
                    "commentContent": "主评论",
                    "commentTime": "2026-04-23 12:00:00",
                    "replyCount": 4,
                },
                {
                    "commentId": "reply-1",
                    "rootCommentId": "root-comment",
                    "commentContent": "第一条回复",
                    "commentTime": "2026-04-23 12:05:00",
                    "replyToName": "楼主",
                },
                {
                    "commentId": "reply-2",
                    "rootCommentId": "root-comment",
                    "commentContent": "第二条回复",
                    "commentTime": "2026-04-23 12:06:00",
                    "replyToName": "楼主",
                },
                {
                    "commentId": "other-root",
                    "commentContent": "第二个主评论",
                    "commentTime": "2026-04-23 12:10:00",
                },
            ]
        }

        threaded = _normalize_comments(meta, Path("."))

        self.assertEqual(len(threaded), 2)
        self.assertEqual(threaded[0]["id"], "root-comment")
        self.assertEqual(threaded[0]["reply_count"], 4)
        self.assertEqual(len(threaded[0]["replies"]), 2)
        self.assertEqual(threaded[0]["replies"][0]["id"], "reply-1")
        self.assertEqual(threaded[0]["replies"][1]["id"], "reply-2")
        self.assertEqual(threaded[1]["id"], "other-root")

    def test_profile_backfill_detects_models_missing_comment_replies(self):
        meta = {
            "instances": [
                {
                    "profileDetailVersion": PROFILE_DETAIL_SCHEMA_VERSION,
                    "pictures": [{"url": "https://example.com/preview.jpg"}],
                }
            ],
            "comments": [
                {
                    "id": "root-comment",
                    "content": "主评论",
                    "replyCount": 2,
                }
            ],
        }

        self.assertTrue(_meta_needs_profile_backfill(meta))

    def test_profile_backfill_skips_comment_reply_rescan_after_schema_upgrade(self):
        meta = {
            "commentSchemaVersion": COMMENT_SCHEMA_VERSION,
            "instances": [
                {
                    "profileDetailVersion": PROFILE_DETAIL_SCHEMA_VERSION,
                    "pictures": [{"url": "https://example.com/preview.jpg"}],
                }
            ],
            "comments": [
                {
                    "id": "root-comment",
                    "content": "主评论",
                    "replyCount": 2,
                }
            ],
        }

        self.assertFalse(_meta_needs_profile_backfill(meta))


if __name__ == "__main__":
    unittest.main()
