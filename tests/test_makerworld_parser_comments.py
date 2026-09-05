from app.services import legacy_archiver
from app.services.makerworld_parsers.comments import (
    extract_comment_list_items,
    extract_comment_replies,
    extract_comment_sections,
    normalize_threaded_comments,
    resolve_comment_count,
)


def test_comments_parser_attaches_flat_replies_to_root():
    items = [
        {"commentId": "root", "commentContent": "主评论", "commentTime": "2026-09-05 10:00:00"},
        {"commentId": "reply", "rootCommentId": "root", "commentContent": "回复", "commentTime": "2026-09-05 10:01:00"},
    ]

    result = normalize_threaded_comments(items)

    assert [item["id"] for item in result] == ["root"]
    assert [item["id"] for item in result[0]["replies"]] == ["reply"]


def test_comments_parser_preserves_blank_rating_comment():
    result = extract_comment_list_items(
        {
            "hits": [
                {
                    "ratingItem": {
                        "id": "rating-root",
                        "score": 5,
                        "instanceId": "profile-1",
                        "content": "",
                        "createTime": "2026-04-23 12:00:00",
                        "creator": {"name": "评分用户"},
                    }
                }
            ]
        }
    )

    assert [(item["id"], item["content"], item["rating"]) for item in result] == [("rating-root", "", 5)]


def test_comments_parser_ignores_empty_and_placeholder_payloads():
    result = extract_comment_list_items(
        {
            "comments": [
                {"replyCount": 0, "content": "Model Description"},
                {"commentId": "empty", "commentContent": ""},
            ]
        }
    )

    assert result == []


def test_comments_parser_preserves_nested_replies():
    result = extract_comment_list_items(
        {
            "comments": [
                {
                    "commentId": "root",
                    "commentContent": "主评论",
                    "commentTime": "2026-04-23 12:00:00",
                    "subCommentVOList": {
                        "records": [
                            {
                                "commentId": "reply",
                                "commentContent": "嵌套回复",
                                "commentTime": "2026-04-23 12:01:00",
                                "replyToName": "楼主",
                            }
                        ]
                    },
                }
            ]
        }
    )

    assert [item["id"] for item in result[0]["replies"]] == ["reply"]


def test_comments_parser_deduplicates_replies_and_extracts_reply_payload():
    payload = {
        "comment": {"id": "root", "content": "主评论", "replyCount": 1},
        "replies": [
            {"id": "reply", "content": "回复", "createTime": "2026-04-23 12:01:00", "replyCount": 0},
            {"id": "reply", "content": "回复", "createTime": "2026-04-23 12:01:00", "replyCount": 0},
        ],
    }

    result = extract_comment_replies(payload, "root")

    assert [(item["id"], item["rootCommentId"]) for item in result] == [("reply", "root")]


def test_comments_parser_extracts_comment_sections_and_resolves_count_priority():
    next_data = {"props": {"pageProps": {"comments": {"totalComments": 3, "items": []}}}}
    sections = extract_comment_sections(next_data)

    assert sections == [{"totalComments": 3, "items": []}]
    assert resolve_comment_count(
        unique_sections=sections,
        next_data=next_data,
        design={"commentCount": 5},
        comment_total=2,
        page_fetch_stats={"total_known": True, "total": 7},
    ) == 7
    assert resolve_comment_count(
        unique_sections=sections,
        next_data=next_data,
        design={"commentCount": 5},
        comment_total=2,
        page_fetch_stats={},
    ) == 5
    assert resolve_comment_count(
        unique_sections=sections,
        next_data=next_data,
        design={},
        comment_total=2,
        page_fetch_stats={},
    ) == 3


def test_comments_parser_root_duplicate_keeps_fields_from_complete_record():
    result = normalize_threaded_comments(
        [
            {
                "id": "root",
                "author": {"name": "旧作者"},
                "content": "旧内容",
                "createdAt": "2026-04-23 12:00:00",
                "replyCount": 0,
                "images": [{"url": "https://example.test/original.jpg"}],
                "sourceOnly": "保留字段",
            },
            {
                "id": "root",
                "author": {"name": "新作者"},
                "content": "新内容",
                "createdAt": "2026-04-23 12:01:00",
                "replyCount": 0,
            },
        ]
    )

    assert len(result) == 1
    assert result[0]["content"] == "新内容"
    assert result[0]["images"] == [{"url": "https://example.test/original.jpg"}]
    assert result[0]["sourceOnly"] == "保留字段"


def test_comments_parser_nested_duplicate_keeps_fields_from_complete_reply():
    result = extract_comment_list_items(
        {
            "comments": [
                {
                    "commentId": "root",
                    "commentContent": "主评论",
                    "commentTime": "2026-04-23 12:00:00",
                    "subCommentVOList": {
                        "records": [
                            {
                                "commentId": "reply",
                                "commentContent": "旧回复",
                                "commentTime": "2026-04-23 12:01:00",
                                "replyToName": "楼主",
                                "pictures": ["https://example.test/reply.jpg"],
                            },
                            {
                                "commentId": "reply",
                                "commentContent": "新回复",
                                "commentTime": "2026-04-23 12:02:00",
                            },
                        ]
                    },
                }
            ]
        }
    )

    reply = result[0]["replies"][0]
    assert reply["content"] == "新回复"
    assert reply["replyToName"] == "楼主"
    assert reply["images"] == [{"url": "https://example.test/reply.jpg"}]


def test_comments_parser_reply_duplicate_keeps_fields_from_complete_record():
    result = normalize_threaded_comments(
        [
            {
                "id": "root",
                "author": {"name": "楼主"},
                "content": "主评论",
                "createdAt": "2026-04-23 12:00:00",
                "replyCount": 1,
                "replies": [
                    {
                        "id": "reply",
                        "author": {"name": "旧作者"},
                        "content": "旧回复",
                        "createdAt": "2026-04-23 12:01:00",
                        "replyCount": 0,
                        "sourceOnly": "保留字段",
                    }
                ],
            },
            {
                "id": "reply",
                "author": {"name": "新作者"},
                "content": "新回复",
                "createdAt": "2026-04-23 12:02:00",
                "replyCount": 0,
                "rootCommentId": "root",
            },
        ]
    )

    reply = result[0]["replies"][0]
    assert reply["content"] == "新回复"
    assert reply["sourceOnly"] == "保留字段"


def test_comments_parser_deduplicates_partial_payload_records_without_losing_old_fields():
    result = extract_comment_list_items(
        {
            "comments": [
                {
                    "commentId": "root",
                    "commentContent": "旧内容",
                    "commentTime": "2026-04-23 12:00:00",
                    "pictures": ["https://example.test/root.jpg"],
                    "replyToName": "旧目标",
                },
                {
                    "commentId": "root",
                    "commentContent": "新内容",
                    "commentTime": "2026-04-23 12:01:00",
                },
            ]
        }
    )

    assert len(result) == 1
    assert result[0]["content"] == "新内容"
    assert result[0]["images"] == [{"url": "https://example.test/root.jpg"}]
    assert result[0]["replyToName"] == "旧目标"


def test_comments_parser_rejects_non_comment_id_and_content_node():
    assert extract_comment_list_items(
        {"data": {"id": "model-1", "content": "这只是模型元数据"}}
    ) == []


def test_comments_parser_handles_none_mixed_values_and_malformed_containers():
    assert normalize_threaded_comments(None) == []
    assert extract_comment_sections(None) == []
    assert extract_comment_replies(None, "root") == []
    assert extract_comment_list_items(None) == []

    payload = {
        "comments": [
            None,
            "invalid",
            7,
            {"items": "not-a-list", "records": {"id": "metadata", "content": "not a comment"}},
            {"commentId": "root", "commentContent": "有效评论", "commentTime": "2026-04-23 12:00:00"},
        ]
    }
    assert [item["id"] for item in extract_comment_list_items(payload)] == ["root"]


def test_legacy_comments_facade_is_parser_function_and_keeps_schema_version():
    items = [
        {"commentId": "root", "commentContent": "主评论", "commentTime": "2026-09-05 10:00:00"},
        {"commentId": "reply", "rootCommentId": "root", "commentContent": "回复", "commentTime": "2026-09-05 10:01:00"},
    ]

    assert legacy_archiver.COMMENT_SCHEMA_VERSION == 4
    assert legacy_archiver.normalize_threaded_comments is normalize_threaded_comments
    assert legacy_archiver.normalize_threaded_comments(items) == normalize_threaded_comments(items)
