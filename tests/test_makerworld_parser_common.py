from app.services.makerworld_parsers.common import (
    extract_model_id,
    normalize_model_url,
    normalize_source_url,
    platform_from_url,
)


def test_common_parser_keeps_cn_and_global_separate():
    assert normalize_source_url("https://makerworld.com/@Ace/upload?x=1") == "https://makerworld.com/zh/@Ace/upload"
    assert normalize_source_url("https://makerworld.com.cn/@Ace") == "https://makerworld.com.cn/zh/@Ace/upload"
    assert normalize_model_url("https://makerworld.com/en/models/123-demo") == "https://makerworld.com/zh/models/123"
    assert extract_model_id("https://makerworld.com.cn/zh/models/456-demo") == "456"
    assert platform_from_url("https://api.bambulab.cn/v1/design-service/design/456") == "cn"
    assert platform_from_url("https://makerworld.com/zh/models/123") == "global"
    assert platform_from_url("https://example.test/models/123") == ""
