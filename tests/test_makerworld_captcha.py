from __future__ import annotations

import base64
import importlib
import importlib.util
import json
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from app.services import makerworld_browser_client as client


CAPTCHA_ID = "a" * 32
TARGET = {"model_id": "123", "model_url": "https://makerworld.com.cn/zh/models/123", "instance_id": "456"}
PROOF = {
    "captcha_id": CAPTCHA_ID,
    "lot_number": "b" * 32,
    "pass_token": "c" * 64,
    "gen_time": "1794989000",
    "captcha_output": "output_123-ABC",
}


def captcha():
    assert importlib.util.find_spec("app.services.makerworld_captcha") is not None, "MW 验证码 helper 尚未实现"
    return importlib.import_module("app.services.makerworld_captcha")


def test_challenge_configuration_exposes_only_validated_public_sdk_fields():
    config = captcha().challenge_config({
        "captchaId": CAPTCHA_ID,
        "apiServers": ["gcaptcha4.geetest.com", "gcaptcha4.gsensebot.com"],
        "staticServers": ["static.geevisit.com"],
        "cookie": "private-cookie",
    })
    assert config == {
        "captcha_id": CAPTCHA_ID,
        "api_servers": ["gcaptcha4.geetest.com", "gcaptcha4.gsensebot.com"],
        "static_servers": ["static.geevisit.com"],
    }


def test_challenge_configuration_uses_official_defaults_and_accepts_snake_case():
    default = captcha().challenge_config({"captchaId": CAPTCHA_ID})
    assert default["captcha_id"] == CAPTCHA_ID
    assert default["api_servers"]
    assert default["static_servers"]
    assert captcha().challenge_config({
        "captcha_id": CAPTCHA_ID,
        "api_servers": ["gcaptcha4.geevisit.com"],
        "static_servers": ["static.geetest.com"],
    }) == {"captcha_id": CAPTCHA_ID, "api_servers": ["gcaptcha4.geevisit.com"], "static_servers": ["static.geetest.com"]}


@pytest.mark.parametrize("payload", [
    None, [], {}, {"captchaId": 123}, {"captchaId": "private-invalid-id"},
    {"captchaId": "a" * 31}, {"captchaId": "z" * 32},
    {"captchaId": CAPTCHA_ID, "apiServers": "gcaptcha4.geetest.com"},
    {"captchaId": CAPTCHA_ID, "apiServers": ["gcaptcha4.geetest.com.attacker.test"]},
    {"captchaId": CAPTCHA_ID, "apiServers": ["https://gcaptcha4.geetest.com"]},
    {"captchaId": CAPTCHA_ID, "apiServers": ["gcaptcha4.geetest.com/path"]},
    {"captchaId": CAPTCHA_ID, "apiServers": ["gcaptcha4.geetest.com:443"]},
    {"captchaId": CAPTCHA_ID, "apiServers": ["localhost"]},
    {"captchaId": CAPTCHA_ID, "staticServers": ["127.0.0.1"]},
    {"captchaId": CAPTCHA_ID, "staticServers": ["static.geetest.com", "attacker.test"]},
    {"captchaId": CAPTCHA_ID, "apiServers": []},
    {"captchaId": CAPTCHA_ID, "staticServers": [None]},
])
def test_invalid_or_untrusted_challenge_configuration_is_not_exposed(payload):
    assert captcha().challenge_config(payload) is None


def test_proof_is_bound_to_challenge_and_uses_standard_base64_json():
    encoded = captcha().encode_proof(PROOF, CAPTCHA_ID)
    decoded = base64.b64decode(encoded, validate=True)
    assert json.loads(decoded) == PROOF
    assert base64.b64encode(decoded).decode("ascii") == encoded


@pytest.mark.parametrize("field,value", [
    ("captcha_id", "d" * 32), ("captcha_id", "invalid"),
    ("lot_number", None), ("lot_number", ""), ("lot_number", "x" * 129),
    ("pass_token", "x" * 513),
    ("pass_token", 1), ("pass_token", True), ("pass_token", "private\r\nheader"),
    ("pass_token", "private\tvalue"), ("pass_token", "含隐私值"),
    ("gen_time", "not-a-time"), ("gen_time", 1794989000),
    ("captcha_output", "x" * 8193), ("captcha_output", "a b"),
])
def test_invalid_proof_is_rejected_without_echoing_ticket_values(field, value):
    with pytest.raises(ValueError) as raised:
        captcha().encode_proof({**PROOF, field: value}, CAPTCHA_ID)
    assert str(raised.value) == "验证码结果无效，请重新验证。"


@pytest.mark.parametrize("proof", [None, [], {}, {key: value for key, value in PROOF.items() if key != "lot_number"},
                                    {**PROOF, "url": "https://attacker.test"}])
def test_proof_requires_exactly_five_fields(proof):
    with pytest.raises(ValueError, match="验证码结果无效"):
        captcha().encode_proof(proof, CAPTCHA_ID)


@pytest.mark.parametrize("platform,host", [("cn", "makerworld.com.cn"), ("global", "makerworld.com")])
def test_authorization_delegates_fixed_same_origin_url_to_single_managed_request(platform, host):
    module = captcha()
    header = module.encode_proof(PROOF, CAPTCHA_ID)
    response = SimpleNamespace(status_code=200, text=json.dumps({"url": "https://cdn.test/model.3mf"}),
                               content_type="application/json", headers={"retry-after": "10", "Set-Cookie": "private"})
    proxy = object()
    with patch.object(client, "makerworld_browser_managed_authorization", return_value=response) as fetch:
        result = module.fetch_authorization(platform, "profile-one", TARGET, proxy_config=proxy, proof_header=header)
    args, kwargs = fetch.call_args
    assert args == (platform, f"https://{host}/api/v1/design-service/instance/456/f3mf?type=download&fileType=3mf")
    assert kwargs["profile_id"] == "profile-one"
    assert kwargs["proxy_config"] is proxy
    assert kwargs["headers"]["x-bbl-captcha-result"] == header
    assert kwargs["timeout_seconds"] == 30
    assert result == {"status_code": 200, "payload": {"url": "https://cdn.test/model.3mf"}, "headers": {"retry-after": "10"}}
    fetch.assert_called_once()


@pytest.mark.parametrize("status,content_type,text,headers", [
    (403, "text/html", "<title>Just a moment...</title><div>private</div>", {}),
    (503, "text/plain", "private-content", {"CF-Mitigated": "challenge"}),
    (200, "text/html", "<html><script src='/cdn-cgi/challenge-platform/a'></script>private</html>", {}),
])
def test_remaining_cloudflare_response_returns_only_safe_marker(status, content_type, text, headers):
    response = SimpleNamespace(status_code=status, content_type=content_type, text=text, headers=headers)
    with patch.object(client, "makerworld_browser_managed_authorization", return_value=response):
        result = captcha().fetch_authorization("cn", "profile-one", TARGET)
    assert result["payload"] == {"code": "MAKERHUB_CLOUDFLARE"}
    assert "private" not in str(result)


def test_mw_json_captcha_is_not_misclassified_as_cloudflare_by_its_message():
    payload = {"captchaId": CAPTCHA_ID, "message": "Please verify you are human"}
    response = SimpleNamespace(status_code=418, content_type="application/json", text=json.dumps(payload), headers={})
    with patch.object(client, "makerworld_browser_managed_authorization", return_value=response):
        result = captcha().fetch_authorization("cn", "profile-one", TARGET)
    assert result["payload"] == payload


@pytest.mark.parametrize("platform,profile,target,header", [
    ("other", "profile-one", TARGET, ""),
    ("cn", "", TARGET, ""),
    ("cn", "profile-one", {**TARGET, "instance_id": "456/../../secret"}, ""),
    ("cn", "profile-one", {**TARGET, "instance_id": "-1"}, ""),
    ("cn", "profile-one", {**TARGET, "instance_id": True}, ""),
    ("cn", "profile-one", TARGET, "private\r\nheader"),
])
def test_invalid_authorization_arguments_never_reach_browser(platform, profile, target, header):
    with patch.object(client, "makerworld_browser_managed_authorization") as fetch:
        with pytest.raises(ValueError):
            captcha().fetch_authorization(platform, profile, target, proof_header=header)
    fetch.assert_not_called()


@pytest.mark.parametrize("text", ["private invalid json", '"private"', "[]", "", "{" + "x" * (256 * 1024)],
                         ids=["invalid-json", "string", "list", "empty", "oversized"])
def test_non_object_or_oversized_authorization_response_does_not_return_body(text):
    response = SimpleNamespace(status_code=200, text=text, content_type="application/json", headers={})
    with patch.object(client, "makerworld_browser_managed_authorization", return_value=response):
        result = captcha().fetch_authorization("cn", "profile-one", TARGET)
    assert result == {"status_code": 200, "payload": {"code": "MAKERHUB_INVALID_AUTHORIZATION_RESPONSE"}, "headers": {}}


def test_authorization_transport_failure_is_not_retried_or_converted_to_success():
    with patch.object(client, "makerworld_browser_managed_authorization", side_effect=client.MakerWorldBrowserError("CloakBrowser 请求失败。")) as fetch:
        with pytest.raises(client.MakerWorldBrowserError):
            captcha().fetch_authorization("cn", "profile-one", TARGET)
    fetch.assert_called_once()
