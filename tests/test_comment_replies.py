import unittest
from pathlib import Path

from app.services.archive_profile_backfill import _meta_needs_profile_backfill
from app.services.catalog import _normalize_comment_item, _normalize_comments
from app.services.legacy_archiver import (
    COMMENT_SCHEMA_VERSION,
    PROFILE_DETAIL_SCHEMA_VERSION,
    _collect_comment_tree,
    collect_comments,
    normalize_threaded_comments,
)


class CommentRepliesTest(unittest.TestCase):
    class _DummyResponse:
        def __init__(self, payload, status_code: int = 200):
            self._payload = payload
            self.status_code = status_code
            self.text = "{}"

        def json(self):
            return self._payload

    class _DummySession:
        def __init__(self, payload):
            self.headers = {"User-Agent": "MakerHub Test"}
            self._payload = payload
            self.calls = []

        def get(self, url, params=None, headers=None, timeout=None):
            self.calls.append(
                {
                    "url": url,
                    "params": params,
                    "headers": headers,
                    "timeout": timeout,
                }
            )
            if callable(self._payload):
                payload = self._payload(url, params or {}, headers or {}, timeout)
                return CommentRepliesTest._DummyResponse(payload)
            return CommentRepliesTest._DummyResponse(self._payload)

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

    def test_archiver_collects_makerworld_comment_reply_field(self):
        comment = {
            "commentId": "root-comment",
            "commentContent": "主评论",
            "commentTime": "2026-04-23 12:00:00",
            "replyCount": 1,
            "commentReply": [
                {
                    "id": "reply-comment",
                    "content": "MakerWorld commentReply 回复",
                    "createTime": "2026-04-23 12:05:00",
                    "replyCount": 0,
                    "likeCount": 0,
                }
            ],
        }

        normalized, is_new = _collect_comment_tree(comment, {})

        self.assertTrue(is_new)
        self.assertIsNotNone(normalized)
        self.assertEqual(normalized["replyCount"], 1)
        self.assertEqual(len(normalized["replies"]), 1)
        self.assertEqual(normalized["replies"][0]["content"], "MakerWorld commentReply 回复")

    def test_archiver_ignores_page_labels_that_look_like_comments(self):
        for comment in (
            {"score": 5, "description": "模型描述"},
            {"rating": 0, "content": "评论"},
            {"replyCount": 0, "content": "Model Description"},
        ):
            normalized, is_new = _collect_comment_tree(comment, {})

            self.assertFalse(is_new)
            self.assertIsNone(normalized)

    def test_archiver_keeps_real_comment_even_when_content_is_short(self):
        comment = {
            "commentId": "real-comment",
            "commentContent": "评论",
            "commentTime": "2026-04-23 12:00:00",
        }

        normalized, is_new = _collect_comment_tree(comment, {})

        self.assertTrue(is_new)
        self.assertIsNotNone(normalized)
        self.assertEqual(normalized["content"], "评论")

    def test_catalog_hides_stored_page_label_comments(self):
        meta = {
            "comments": [
                {
                    "id": "generated-placeholder-id",
                    "author": {"name": ""},
                    "content": "Model Description",
                    "likeCount": 0,
                    "replyCount": 0,
                },
                {
                    "commentId": "real-comment",
                    "author": {"name": "用户"},
                    "content": "Model Description",
                    "commentTime": "2026-04-23 12:00:00",
                },
            ]
        }

        normalized = _normalize_comments(meta, Path("."))

        self.assertEqual(len(normalized), 1)
        self.assertEqual(normalized[0]["id"], "real-comment")

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

    def test_catalog_threads_sequential_flattened_replies_without_root_comment_id(self):
        meta = {
            "comments": [
                {
                    "commentId": "root-comment",
                    "commentContent": "主评论",
                    "commentTime": "2026-04-23 12:00:00",
                    "replyCount": 2,
                },
                {
                    "commentId": "reply-1",
                    "commentContent": "第一条顺序回复",
                    "commentTime": "2026-04-23 12:05:00",
                    "replyToName": "楼主",
                },
                {
                    "commentId": "reply-2",
                    "commentContent": "第二条顺序回复",
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
        self.assertEqual(threaded[0]["reply_count"], 2)
        self.assertEqual(len(threaded[0]["replies"]), 2)
        self.assertEqual(threaded[0]["replies"][0]["id"], "reply-1")
        self.assertEqual(threaded[0]["replies"][1]["id"], "reply-2")
        self.assertEqual(threaded[1]["id"], "other-root")

    def test_archiver_threads_flattened_replies_before_saving(self):
        comments = [
            {
                "id": "root-comment",
                "content": "主评论",
                "createdAt": "2026-04-23 12:00:00",
                "replyCount": 2,
            },
            {
                "id": "reply-1",
                "rootCommentId": "root-comment",
                "content": "第一条回复",
                "createdAt": "2026-04-23 12:05:00",
                "replyToName": "楼主",
            },
            {
                "id": "reply-2",
                "content": "第二条顺序回复",
                "createdAt": "2026-04-23 12:06:00",
                "replyToName": "楼主",
            },
            {
                "id": "other-root",
                "content": "第二个主评论",
                "createdAt": "2026-04-23 12:10:00",
            },
        ]

        threaded = normalize_threaded_comments(comments)

        self.assertEqual(len(threaded), 2)
        self.assertEqual(threaded[0]["id"], "root-comment")
        self.assertEqual(threaded[0]["replyCount"], 2)
        self.assertEqual([item["id"] for item in threaded[0]["replies"]], ["reply-1", "reply-2"])
        self.assertEqual(threaded[1]["id"], "other-root")

    def test_collect_comments_fetches_missing_replies_from_comment_api(self):
        next_data = {
            "comments": [
                {
                    "commentId": "root-comment",
                    "commentContent": "主评论",
                    "commentTime": "2026-04-23 12:00:00",
                    "replyCount": 2,
                }
            ]
        }
        design = {
            "url": "https://makerworld.com.cn/zh/models/123456",
            "commentCount": 3,
        }
        reply_payload = {
            "data": {
                "items": [
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
                ],
                "hasNext": False,
            }
        }
        session = self._DummySession(reply_payload)

        bundle = collect_comments(
            next_data,
            design,
            session,
            Path("."),
            download_assets=False,
        )

        self.assertEqual(bundle["count"], 3)
        self.assertEqual(len(bundle["items"]), 1)
        self.assertEqual(bundle["items"][0]["replyCount"], 2)
        self.assertEqual([item["id"] for item in bundle["items"][0]["replies"]], ["reply-1", "reply-2"])
        reply_calls = [call for call in session.calls if "/comment-service/comment/root-comment/reply" in call["url"]]
        self.assertEqual(len(reply_calls), 1)
        self.assertEqual(reply_calls[0]["params"], {"limit": 20})

    def test_collect_comments_accepts_reply_api_top_level_replies(self):
        next_data = {
            "comments": [
                {
                    "commentId": "root-comment",
                    "commentContent": "主评论",
                    "commentTime": "2026-04-23 12:00:00",
                    "replyCount": 2,
                }
            ]
        }
        design = {
            "url": "https://makerworld.com.cn/zh/models/123456",
            "commentCount": 3,
        }
        reply_payload = {
            "comment": {
                "id": "root-comment",
                "content": "主评论",
                "createTime": "2026-04-23 12:00:00",
                "replyCount": 2,
            },
            "replies": [
                {
                    "id": "reply-1",
                    "content": "第一条顶层 replies 回复",
                    "createTime": "2026-04-23 12:05:00",
                    "replyCount": 0,
                    "likeCount": 0,
                },
                {
                    "id": "reply-2",
                    "content": "第二条顶层 replies 回复",
                    "createTime": "2026-04-23 12:06:00",
                    "replyCount": 0,
                    "likeCount": 0,
                },
            ],
        }
        session = self._DummySession(reply_payload)

        bundle = collect_comments(
            next_data,
            design,
            session,
            Path("."),
            download_assets=False,
        )

        self.assertEqual(bundle["count"], 3)
        self.assertEqual(len(bundle["items"]), 1)
        self.assertEqual(bundle["items"][0]["replyCount"], 2)
        self.assertEqual([item["id"] for item in bundle["items"][0]["replies"]], ["reply-1", "reply-2"])
        self.assertEqual([item["rootCommentId"] for item in bundle["items"][0]["replies"]], ["root-comment", "root-comment"])

    def test_collect_comments_hydrates_existing_comments_when_fresh_payload_has_no_comments(self):
        design = {
            "url": "https://makerworld.com.cn/zh/models/123456",
            "commentCount": 2,
        }
        existing_comments = [
            {
                "id": "root-comment",
                "content": "本地已有主评论",
                "createdAt": "2026-04-23 12:00:00",
                "replyCount": 1,
            }
        ]
        reply_payload = {
            "replies": [
                {
                    "id": "reply-1",
                    "content": "补回来的回复",
                    "createTime": "2026-04-23 12:05:00",
                    "replyCount": 0,
                    "likeCount": 0,
                }
            ],
        }
        session = self._DummySession(reply_payload)

        bundle = collect_comments(
            {},
            design,
            session,
            Path("."),
            download_assets=False,
            existing_comments=existing_comments,
        )

        self.assertEqual(bundle["count"], 2)
        self.assertEqual(len(bundle["items"]), 1)
        self.assertEqual(bundle["items"][0]["id"], "root-comment")
        self.assertEqual(bundle["items"][0]["replies"][0]["content"], "补回来的回复")

    def test_collect_comments_fetches_paginated_commentandrating_pages(self):
        def payload(url, params, _headers, _timeout):
            if "commentandrating" not in url:
                return {}
            offset = int(params.get("offset") or 0)
            if offset == 0:
                return {
                    "total": 3,
                    "hits": [
                        {
                            "type": 1,
                            "comment": {
                                "id": "comment-1",
                                "content": "第一页评论",
                                "createTime": "2026-04-23 12:00:00",
                                "replyCount": 0,
                                "user": {"name": "用户一"},
                            },
                        },
                        {
                            "type": 2,
                            "ratingItem": {
                                "id": "rating-1",
                                "score": 5,
                                "instanceId": "profile-1",
                                "content": "第一页评分",
                                "createTime": "2026-04-23 12:01:00",
                                "replyCount": 0,
                                "creator": {"name": "用户二"},
                            },
                        },
                    ],
                }
            if offset == 2:
                return {
                    "total": 3,
                    "hits": [
                        {
                            "type": 1,
                            "comment": {
                                "id": "comment-2",
                                "content": "第二页评论",
                                "createTime": "2026-04-23 12:02:00",
                                "replyCount": 0,
                                "user": {"name": "用户三"},
                            },
                        }
                    ],
                }
            return {"total": 3, "hits": []}

        session = self._DummySession(payload)

        bundle = collect_comments(
            {},
            {"id": "123456", "url": "https://makerworld.com.cn/zh/models/123456", "commentCount": 3},
            session,
            Path("."),
            download_assets=False,
        )

        self.assertEqual(bundle["count"], 3)
        self.assertEqual([item["id"] for item in bundle["items"]], ["comment-1", "rating-1", "comment-2"])
        page_calls = [call for call in session.calls if "commentandrating" in call["url"]]
        self.assertEqual([call["params"]["offset"] for call in page_calls], [0, 2])

    def test_collect_comments_hydrates_rating_replies_from_rating_api(self):
        def payload(url, params, _headers, _timeout):
            if "commentandrating" in url:
                return {
                    "total": 1,
                    "hits": [
                        {
                            "type": 2,
                            "ratingItem": {
                                "id": "rating-root",
                                "score": 5,
                                "instanceId": "profile-1",
                                "content": "带回复的评分",
                                "createTime": "2026-04-23 12:00:00",
                                "replyCount": 3,
                                "creator": {"name": "评分用户"},
                                "instRatingReply": [
                                    {
                                        "id": "rating-reply-1",
                                        "ratingId": "rating-root",
                                        "content": "已有评分回复",
                                        "createTime": "2026-04-23 12:05:00",
                                        "replyCount": 0,
                                        "creator": {"name": "回复用户一"},
                                    }
                                ],
                            },
                        }
                    ],
                }
            if "/rating/rating-root/reply" in url:
                return {
                    "total": 3,
                    "hits": [
                        {
                            "id": "rating-reply-1",
                            "ratingId": "rating-root",
                            "content": "已有评分回复",
                            "createTime": "2026-04-23 12:05:00",
                            "replyCount": 0,
                            "creator": {"name": "回复用户一"},
                        },
                        {
                            "id": "rating-reply-2",
                            "ratingId": "rating-root",
                            "content": "补全评分回复二",
                            "createTime": "2026-04-23 12:06:00",
                            "replyCount": 0,
                            "creator": {"name": "回复用户二"},
                        },
                        {
                            "id": "rating-reply-3",
                            "ratingId": "rating-root",
                            "content": "补全评分回复三",
                            "createTime": "2026-04-23 12:07:00",
                            "replyCount": 0,
                            "creator": {"name": "回复用户三"},
                        },
                    ],
                }
            return {}

        session = self._DummySession(payload)

        bundle = collect_comments(
            {},
            {"id": "123456", "url": "https://makerworld.com.cn/zh/models/123456", "commentCount": 1},
            session,
            Path("."),
            download_assets=False,
        )

        self.assertEqual(len(bundle["items"]), 1)
        self.assertEqual(bundle["items"][0]["id"], "rating-root")
        self.assertEqual(bundle["items"][0]["commentSource"], "rating")
        self.assertEqual([item["id"] for item in bundle["items"][0]["replies"]], [
            "rating-reply-1",
            "rating-reply-2",
            "rating-reply-3",
        ])
        self.assertTrue(any("/rating/rating-root/reply" in call["url"] for call in session.calls))
        self.assertFalse(any("/comment/rating-root/reply" in call["url"] for call in session.calls))

    def test_collect_comments_keeps_blank_rating_items(self):
        def payload(url, params, _headers, _timeout):
            if "commentandrating" in url:
                return {
                    "total": 1,
                    "hits": [
                        {
                            "type": 2,
                            "ratingItem": {
                                "id": "blank-rating",
                                "score": 5,
                                "instanceId": "profile-1",
                                "content": "",
                                "createTime": "2026-04-23 12:00:00",
                                "replyCount": 0,
                                "creator": {"name": "评分用户"},
                            },
                        }
                    ],
                }
            return {}

        session = self._DummySession(payload)

        bundle = collect_comments(
            {},
            {"id": "123456", "url": "https://makerworld.com.cn/zh/models/123456", "commentCount": 1},
            session,
            Path("."),
            download_assets=False,
        )

        self.assertEqual(len(bundle["items"]), 1)
        self.assertEqual(bundle["items"][0]["id"], "blank-rating")
        self.assertEqual(bundle["items"][0]["content"], "")
        self.assertEqual(bundle["items"][0]["rating"], 5)

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

    def test_profile_backfill_detects_flattened_top_level_reply_items(self):
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
                },
                {
                    "id": "reply-1",
                    "rootCommentId": "root-comment",
                    "content": "第一条回复",
                    "replyToName": "楼主",
                },
            ],
        }

        self.assertTrue(_meta_needs_profile_backfill(meta))

    def test_profile_backfill_detects_missing_replies_even_after_schema_upgrade(self):
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

        self.assertTrue(_meta_needs_profile_backfill(meta))

    def test_profile_backfill_detects_partial_comment_pages(self):
        meta = {
            "commentSchemaVersion": COMMENT_SCHEMA_VERSION,
            "commentCount": 3,
            "instances": [
                {
                    "profileDetailVersion": PROFILE_DETAIL_SCHEMA_VERSION,
                    "pictures": [{"url": "https://example.com/preview.jpg"}],
                }
            ],
            "comments": [
                {
                    "id": "root-comment",
                    "content": "只存了一条评论",
                    "replyCount": 0,
                }
            ],
        }

        self.assertTrue(_meta_needs_profile_backfill(meta))

    def test_profile_backfill_skips_complete_replies_after_schema_upgrade(self):
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
                    "replyCount": 1,
                    "replies": [
                        {
                            "id": "reply-1",
                            "content": "第一条回复",
                        }
                    ],
                }
            ],
        }

        self.assertFalse(_meta_needs_profile_backfill(meta))


if __name__ == "__main__":
    unittest.main()
