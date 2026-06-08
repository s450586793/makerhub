import unittest
from types import SimpleNamespace
from unittest.mock import patch

from app.services import scrapling_fetch


class ScraplingFetchClientTest(unittest.TestCase):
    def test_disabled_engine_does_not_import_scrapling(self):
        result = scrapling_fetch.fetch_text(
            "https://example.test/page",
            advanced_config={"scraping_engine": "legacy"},
        )

        self.assertFalse(result.ok)
        self.assertEqual(result.engine, "disabled")

    def test_static_fetcher_result_is_returned(self):
        calls = []

        class FakeFetcher:
            @classmethod
            def get(cls, url, **kwargs):
                calls.append((url, kwargs))
                return SimpleNamespace(
                    status=200,
                    text="<html><body>ok</body></html>",
                    headers={"content-type": "text/html"},
                    url=url,
                )

        with patch.object(scrapling_fetch, "_load_fetchers", return_value=(FakeFetcher, None, "")):
            result = scrapling_fetch.fetch_text(
                "https://example.test/page",
                raw_cookie="token=abc",
                headers={"User-Agent": "test-agent"},
                advanced_config={"scraping_engine": "scrapling_first", "scrapling_browser_fallback": False},
            )

        self.assertTrue(result.ok)
        self.assertEqual(result.engine, "scrapling-static")
        self.assertEqual(result.text, "<html><body>ok</body></html>")
        self.assertEqual(calls[0][1]["headers"]["Cookie"], "token=abc")
        self.assertEqual(calls[0][1]["headers"]["User-Agent"], "test-agent")

    def test_domestic_target_bypasses_proxy(self):
        calls = []

        class FakeFetcher:
            @classmethod
            def get(cls, url, **kwargs):
                calls.append((url, kwargs))
                return SimpleNamespace(
                    status=200,
                    text="<html><body>ok</body></html>",
                    headers={"content-type": "text/html"},
                    url=url,
                )

        proxy_config = SimpleNamespace(
            enabled=True,
            http_proxy="http://proxy.local:7890",
            https_proxy="http://proxy.local:7891",
            no_proxy="",
        )

        with patch.object(scrapling_fetch, "_load_fetchers", return_value=(FakeFetcher, None, "")):
            result = scrapling_fetch.fetch_text(
                "https://makerworld.com.cn/zh/models/1",
                proxy_config=proxy_config,
                advanced_config={"scraping_engine": "scrapling_first", "scrapling_browser_fallback": False},
            )

        self.assertTrue(result.ok)
        self.assertIsNone(calls[0][1]["proxy"])

    def test_domestic_target_can_use_proxy_for_account_auth(self):
        calls = []

        class FakeFetcher:
            @classmethod
            def get(cls, url, **kwargs):
                calls.append((url, kwargs))
                return SimpleNamespace(
                    status=200,
                    text="<html><body>ok</body></html>",
                    headers={"content-type": "text/html"},
                    url=url,
                )

        proxy_config = SimpleNamespace(
            enabled=True,
            http_proxy="http://proxy.local:7890",
            https_proxy="http://proxy.local:7891",
            no_proxy="",
        )

        with patch.object(scrapling_fetch, "_load_fetchers", return_value=(FakeFetcher, None, "")):
            result = scrapling_fetch.fetch_text(
                "https://api.bambulab.cn/v1/user-service/my/message/count",
                proxy_config=proxy_config,
                advanced_config={"scraping_engine": "scrapling_first", "scrapling_browser_fallback": False},
                allow_domestic_proxy=True,
            )

        self.assertTrue(result.ok)
        self.assertEqual(calls[0][1]["proxy"], "http://proxy.local:7891")

    def test_global_target_uses_proxy(self):
        calls = []

        class FakeFetcher:
            @classmethod
            def get(cls, url, **kwargs):
                calls.append((url, kwargs))
                return SimpleNamespace(
                    status=200,
                    text="<html><body>ok</body></html>",
                    headers={"content-type": "text/html"},
                    url=url,
                )

        proxy_config = SimpleNamespace(
            enabled=True,
            http_proxy="http://proxy.local:7890",
            https_proxy="http://proxy.local:7891",
            no_proxy="",
        )

        with patch.object(scrapling_fetch, "_load_fetchers", return_value=(FakeFetcher, None, "")):
            result = scrapling_fetch.fetch_text(
                "https://makerworld.com/en/models/1",
                proxy_config=proxy_config,
                advanced_config={"scraping_engine": "scrapling_first", "scrapling_browser_fallback": False},
            )

        self.assertTrue(result.ok)
        self.assertEqual(calls[0][1]["proxy"], "http://proxy.local:7891")

    def test_json_fetch_parses_static_response(self):
        class FakeFetcher:
            @classmethod
            def get(cls, url, **kwargs):
                return SimpleNamespace(
                    status=200,
                    text='{"data":{"id":123}}',
                    headers={"content-type": "application/json"},
                    url=url,
                )

        with patch.object(scrapling_fetch, "_load_fetchers", return_value=(FakeFetcher, None, "")):
            result = scrapling_fetch.fetch_json(
                "https://example.test/api",
                advanced_config={"scraping_engine": "scrapling_first", "scrapling_browser_fallback": False},
            )

        self.assertTrue(result.ok)
        self.assertEqual(result.payload["data"]["id"], 123)

    def test_browser_fallback_runs_for_verification_page(self):
        class FakeFetcher:
            @classmethod
            def get(cls, url, **kwargs):
                return SimpleNamespace(
                    status=200,
                    text="<html>verify you are human</html>",
                    headers={"content-type": "text/html"},
                    url=url,
                )

        class FakeStealthyFetcher:
            @classmethod
            def fetch(cls, url, **kwargs):
                return SimpleNamespace(
                    status=200,
                    text='{"ok":true}',
                    headers={"content-type": "application/json"},
                    url=url,
                )

        with patch.object(scrapling_fetch, "_load_fetchers", return_value=(FakeFetcher, FakeStealthyFetcher, "")):
            result = scrapling_fetch.fetch_json(
                "https://example.test/api",
                advanced_config={"scraping_engine": "scrapling_first", "scrapling_browser_fallback": True},
            )

        self.assertTrue(result.ok)
        self.assertEqual(result.engine, "scrapling-browser")
        self.assertEqual(result.payload["ok"], True)


if __name__ == "__main__":
    unittest.main()
