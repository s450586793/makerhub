import json

import pytest

from app.services.makerworld_parsers.model import (
    design_payload_error,
    extract_design_from_next_data,
    extract_next_data,
    is_cloudflare_challenge,
    is_makerworld_not_found_page,
)


def test_model_parser_extracts_matching_design_without_io():
    design = {"id": 2416065, "title": "Demo", "coverUrl": "https://cdn.example.test/a.jpg"}
    html = f'<script id="__NEXT_DATA__" type="application/json">{json.dumps({"props": {"pageProps": {"design": design}}})}</script>'

    next_data = extract_next_data(html)

    assert extract_design_from_next_data(next_data) == design
    assert design_payload_error(design, "https://makerworld.com.cn/zh/models/2416065") == ""


def test_model_parser_rejects_malformed_next_data_script():
    with pytest.raises(RuntimeError, match="未找到 __NEXT_DATA__"):
        extract_next_data('<script id="__NEXT_DATA__">{"props":</script>')


def test_model_parser_extracts_javascript_assignment():
    payload = {"props": {"pageProps": {"design": {"id": 2416065, "title": "Demo"}}}}

    assert extract_next_data(f"<script>window.__NEXT_DATA__ = {json.dumps(payload)};</script>") == payload


def test_model_parser_reports_payload_identity_and_title_errors():
    source_url = "https://makerworld.com.cn/zh/models/2416065"

    assert "不匹配" in design_payload_error({"id": 123, "title": "Wrong model"}, source_url)
    assert "标题为空" in design_payload_error({"id": 2416065, "title": ""}, source_url)


def test_model_parser_detects_cloudflare_and_makerworld_not_found_pages():
    assert is_cloudflare_challenge("<title>Just a moment...</title><div id=\"cf-chl\"></div>")
    assert is_makerworld_not_found_page("<title>404</title>该模型可能被改为草稿、下架或者设为私有。")
