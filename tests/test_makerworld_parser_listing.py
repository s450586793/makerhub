from app.services.makerworld_parsers.listing import (
    extract_collection_entries,
    extract_followed_authors,
    extract_model_source_items,
)


def test_listing_parser_separates_collection_entries_from_nested_designs():
    payload = {"hits": [{
        "id": 518732,
        "title": "默认收藏夹",
        "designCnt": 2,
        "designs": [{"id": 2356866, "title": "嵌套模型"}],
    }]}

    assert extract_collection_entries(payload, "2024907479") == [
        {"id": "518732", "name": "默认收藏夹", "count": 2}
    ]


def test_listing_parser_emits_only_author_hits():
    payload = {"hits": [
        {"uid": 1, "handle": "AcePrint", "name": "Ace", "avatarUrl": "https://example.test/a.jpg"},
        {"designId": 2, "title": "Not author", "coverUrl": "https://example.test/m.jpg"},
    ]}

    assert [item["url"] for item in extract_followed_authors(payload, "cn")] == [
        "https://makerworld.com.cn/zh/@AcePrint/upload"
    ]


def test_listing_parser_builds_model_source_items_in_hit_order():
    payload = {"hits": [{"id": 1002, "title": "模型 B", "coverUrl": "https://example.test/b.jpg"}]}

    assert extract_model_source_items(payload, "https://makerworld.com.cn/zh/@ace/upload") == [{
        "url": "https://makerworld.com.cn/zh/models/1002",
        "model_id": "1002",
        "task_key": "model:1002",
        "source_order": 0,
        "source_position": 0,
    }]
