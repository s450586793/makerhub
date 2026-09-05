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
            {"id": "reply", "content": "回复", "createTime": "2026-04-23 12:01:00"},
            {"id": "reply", "content": "回复", "createTime": "2026-04-23 12:01:00"},
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
