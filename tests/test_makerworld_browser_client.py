import unittest
from multiprocessing import get_context
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch

from app.services import makerworld_browser_client as client
from app.services import resource_limiter
from app.services.cloakbrowser_session import (
    CloakBrowserBridgeError,
    CloakBrowserFetchResult,
    CloakBrowserUnavailable,
)


def _fetch_result(
    *,
    url: str = "https://makerworld.com.cn/zh/models/1",
    status_code: int = 200,
    content_type: str = "text/html; charset=utf-8",
    text: str = "<html>ok</html>",
) -> CloakBrowserFetchResult:
    return CloakBrowserFetchResult(
        profile_id="profile-cn",
        url=url,
        status_code=status_code,
        content_type=content_type,
        text=text,
        headers={"content-type": content_type},
    )


def _read_retry_in_new_process(state_dir, queue):
    client.STATE_DIR = Path(state_dir)
    resource_limiter.STATE_DIR = Path(state_dir)
    delays = []
    client.time = SimpleNamespace(time=lambda: 102.0, sleep=delays.append)
    client.wait_for_makerworld_browser_retry("https://makerworld.com.cn/zh/models/2")
    client.wait_for_makerworld_browser_retry("https://makerworld.com/zh/models/2")
    queue.put(delays)


class MakerWorldBrowserClientTest(unittest.TestCase):
    def setUp(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        for module in (client, resource_limiter):
            patcher = patch.object(module, "STATE_DIR", Path(directory.name), create=True)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.clock = 100.0
        clock_patch = patch.object(client, "time", SimpleNamespace(time=lambda: self.clock, sleep=self.advance_clock), create=True)
        clock_patch.start()
        self.addCleanup(clock_patch.stop)

    def advance_clock(self, seconds):
        self.clock += seconds

    def test_transient_failures_back_off_across_requests_and_reset_after_success(self):
        calls = []
        outcomes = [CloakBrowserUnavailable("HTTP 502"), CloakBrowserUnavailable("HTTP 502"), _fetch_result(), _fetch_result()]

        def fetch(*args, **kwargs):
            calls.append(self.clock)
            result = outcomes.pop(0)
            if isinstance(result, Exception):
                raise result
            return result

        with patch.object(client.JsonStore, "load", return_value=SimpleNamespace(cookies=[])), \
                patch.object(client, "browser_fetch", side_effect=fetch):
            with self.assertRaises(client.MakerWorldBrowserError):
                client.makerworld_browser_get("https://makerworld.com.cn/zh/models/1")
            client.makerworld_browser_get("https://makerworld.com.cn/zh/models/2")
            client.makerworld_browser_get("https://makerworld.com.cn/zh/models/3")
        self.assertEqual(calls, [100.0, 102.0, 107.0, 107.0])

    def test_verification_response_is_not_retried_or_put_on_infrastructure_backoff(self):
        with patch.object(client.JsonStore, "load", return_value=SimpleNamespace(cookies=[])), \
                patch.object(client, "browser_fetch", return_value=_fetch_result(status_code=403)) as fetch:
            response = client.makerworld_browser_get("https://makerworld.com.cn/zh/models/1")
        self.assertEqual(response.status_code, 403)
        self.assertEqual(fetch.call_count, 1)
        self.assertEqual(self.clock, 100.0)

    def test_backoff_survives_a_new_process_and_does_not_block_the_other_site(self):
        with patch.object(client.JsonStore, "load", return_value=SimpleNamespace(cookies=[])), \
                patch.object(client, "browser_fetch", side_effect=CloakBrowserUnavailable("HTTP 502")):
            with self.assertRaises(client.MakerWorldBrowserError):
                client.makerworld_browser_get("https://makerworld.com.cn/zh/models/1")
        context = get_context("spawn")
        queue = context.Queue()
        process = context.Process(target=_read_retry_in_new_process, args=(str(client.STATE_DIR), queue))
        process.start()
        try:
            self.assertEqual(queue.get(timeout=10), [5.0])
            process.join(timeout=10)
            self.assertEqual(process.exitcode, 0)
        finally:
            if process.is_alive():
                process.terminate()
                process.join(timeout=5)
            queue.close()
            queue.join_thread()

    def test_corrupt_or_future_retry_timestamp_cannot_delay_forever(self):
        path = client.STATE_DIR / "browser_get_retry_cn.json"
        for value, expected in [('invalid json', 100.0), ('{"retry_at":"bad"}', 100.0), ('{"retry_at":1e99}', 130.0)]:
            with self.subTest(value=value):
                self.clock = 100.0
                path.write_text(value, encoding="utf-8")
                client.wait_for_makerworld_browser_retry("https://makerworld.com.cn/zh/models/1")
                self.assertEqual(self.clock, expected)

    def test_linked_profile_does_not_seed_stale_cookie(self):
        proxy_config = SimpleNamespace(enabled=False, http_proxy="", https_proxy="")
        config = SimpleNamespace(
            proxy=proxy_config,
            cookies=[
                SimpleNamespace(
                    platform="cn",
                    browser_profile_id="profile-cn",
                    cookie="token=old",
                )
            ]
        )
        with patch.object(client.JsonStore, "load", return_value=config), patch.object(
            client,
            "browser_fetch",
            return_value=_fetch_result(),
        ) as fetch_mock:
            response = client.makerworld_browser_get(
                "https://makerworld.com.cn/zh/models/1",
                raw_cookie="token=old",
                headers={
                    "Accept": "application/json",
                    "Authorization": "Bearer old",
                    "token": "old",
                    "User-Agent": "stale-fingerprint",
                },
            )

        self.assertEqual(response.profile_id, "profile-cn")
        self.assertEqual(fetch_mock.call_args.kwargs["profile_id"], "profile-cn")
        self.assertIs(fetch_mock.call_args.kwargs["proxy_config"], proxy_config)
        self.assertEqual(fetch_mock.call_args.kwargs["cookie_items"], [])
        self.assertEqual(
            fetch_mock.call_args.kwargs["headers"],
            {"Accept": "application/json"},
        )

    def test_unlinked_profile_can_seed_legacy_cookie(self):
        config = SimpleNamespace(
            cookies=[
                SimpleNamespace(
                    platform="cn",
                    browser_profile_id="",
                    cookie="token=legacy",
                )
            ]
        )
        with patch.object(client.JsonStore, "load", return_value=config), patch.object(
            client,
            "browser_fetch",
            return_value=_fetch_result(),
        ) as fetch_mock:
            client.makerworld_browser_get(
                "https://makerworld.com.cn/zh/models/1",
                raw_cookie="token=legacy",
            )

        self.assertEqual(fetch_mock.call_args.kwargs["profile_id"], "")
        self.assertTrue(fetch_mock.call_args.kwargs["cookie_items"])

    def test_get_adds_params_and_infers_global_platform(self):
        config = SimpleNamespace(
            cookies=[SimpleNamespace(platform="global", browser_profile_id="profile-global")]
        )
        result = _fetch_result(
            url="https://api.bambulab.com/v1/design-service/designs?limit=20&tag=a&tag=b",
            content_type="application/json",
            text="{}",
        )
        with patch.object(client.JsonStore, "load", return_value=config), patch.object(
            client,
            "browser_fetch",
            return_value=result,
        ) as fetch_mock:
            client.makerworld_browser_get(
                "https://api.bambulab.com/v1/design-service/designs?limit=10",
                params={"limit": 20, "tag": ["a", "b"], "empty": None},
            )

        args = fetch_mock.call_args.args
        self.assertEqual(args[0], "global")
        self.assertEqual(
            args[1],
            "https://api.bambulab.com/v1/design-service/designs?limit=10&limit=20&tag=a&tag=b",
        )
        self.assertEqual(fetch_mock.call_args.kwargs["profile_id"], "profile-global")

    def test_get_retries_transient_browser_failure_once(self):
        config = SimpleNamespace(cookies=[])
        with patch.object(client.JsonStore, "load", return_value=config), patch.object(
            client,
            "browser_fetch",
            side_effect=[
                CloakBrowserUnavailable("指纹浏览器返回 HTTP 502：upstream unavailable"),
                _fetch_result(),
            ],
        ) as fetch_mock:
            response = client.makerworld_browser_get(
                "https://makerworld.com.cn/zh/models/1"
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(fetch_mock.call_count, 2)

    def test_get_retries_direct_bridge_timeout_once(self):
        config = SimpleNamespace(cookies=[])
        with patch.object(client.JsonStore, "load", return_value=config), patch.object(
            client,
            "browser_fetch",
            side_effect=[
                CloakBrowserBridgeError("指纹浏览器 CDP 操作超时。"),
                _fetch_result(),
            ],
        ) as fetch_mock:
            response = client.makerworld_browser_get(
                "https://makerworld.com.cn/zh/models/1"
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(fetch_mock.call_count, 2)

    def test_get_does_not_expose_query_or_secret_in_error(self):
        config = SimpleNamespace(cookies=[])
        error = CloakBrowserUnavailable(
            "连接指纹浏览器失败：https://makerworld.com.cn/path?token=secret-token"
        )
        with patch.object(client.JsonStore, "load", return_value=config), patch.object(
            client,
            "browser_fetch",
            side_effect=error,
        ) as fetch_mock:
            with self.assertRaises(client.MakerWorldBrowserError) as raised:
                client.makerworld_browser_get(
                    "https://makerworld.com.cn/path?access_token=secret-token",
                    raw_cookie="session=private-cookie",
                )

        message = str(raised.exception)
        self.assertEqual(fetch_mock.call_count, 2)
        self.assertNotIn("secret-token", message)
        self.assertNotIn("private-cookie", message)
        self.assertNotIn("access_token", message)

    def test_get_text_returns_response_body(self):
        with patch.object(
            client,
            "makerworld_browser_get",
            return_value=SimpleNamespace(text="page body"),
        ):
            self.assertEqual(
                client.makerworld_browser_get_text("https://makerworld.com.cn/zh/models/1"),
                "page body",
            )

    def test_get_json_parses_json_response(self):
        response = client.MakerWorldBrowserResponse(
            url="https://api.bambulab.cn/v1/design-service/design/1",
            status_code=200,
            content_type="application/json",
            text='{"hits":[{"id":1}]}',
            profile_id="profile-cn",
        )
        with patch.object(client, "makerworld_browser_get", return_value=response):
            payload = client.makerworld_browser_get_json(response.url)

        self.assertEqual(payload, {"hits": [{"id": 1}]})

    def test_get_json_rejects_html_unless_allow_non_json(self):
        response = client.MakerWorldBrowserResponse(
            url="https://api.bambulab.cn/v1/design-service/design/1",
            status_code=200,
            content_type="text/html",
            text="<html>login</html>",
            profile_id="profile-cn",
        )
        with patch.object(client, "makerworld_browser_get", return_value=response):
            with self.assertRaises(client.MakerWorldBrowserJsonError):
                client.makerworld_browser_get_json(response.url)
            self.assertIsNone(
                client.makerworld_browser_get_json(
                    response.url,
                    allow_non_json=True,
                )
            )


if __name__ == "__main__":
    unittest.main()
