import os
import unittest
from types import SimpleNamespace

from app.services.proxy_policy import (
    is_domestic_proxy_bypass_url,
    proxy_mapping,
    temporary_proxy_env,
)


class ProxyPolicyTest(unittest.TestCase):
    def test_domestic_makerworld_and_bambu_hosts_bypass_proxy(self):
        self.assertTrue(is_domestic_proxy_bypass_url("https://makerworld.com.cn/zh/models/1"))
        self.assertTrue(is_domestic_proxy_bypass_url("https://api.bambulab.cn/v1/design-service/design/1"))
        self.assertFalse(is_domestic_proxy_bypass_url("https://makerworld.com/en/models/1"))
        self.assertFalse(is_domestic_proxy_bypass_url("https://api.bambulab.com/v1/design-service/design/1"))

    def test_proxy_mapping_bypasses_cn_but_keeps_global_proxy(self):
        proxy = SimpleNamespace(
            enabled=True,
            http_proxy="http://proxy.local:7890",
            https_proxy="http://proxy.local:7891",
            no_proxy="localhost",
        )

        self.assertEqual(proxy_mapping(proxy, "https://makerworld.com.cn/zh/models/1"), {})
        self.assertEqual(
            proxy_mapping(proxy, "https://makerworld.com/en/models/1"),
            {"http": "http://proxy.local:7890", "https": "http://proxy.local:7891"},
        )

    def test_temporary_proxy_env_clears_proxy_for_domestic_target(self):
        proxy = {
            "enabled": True,
            "http_proxy": "http://proxy.local:7890",
            "https_proxy": "http://proxy.local:7891",
            "no_proxy": "localhost",
        }
        previous = {key: os.environ.get(key) for key in ("HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY", "http_proxy", "https_proxy", "no_proxy")}
        try:
            os.environ["HTTP_PROXY"] = "http://existing.proxy"
            os.environ["HTTPS_PROXY"] = "http://existing.proxy"
            with temporary_proxy_env(proxy, "https://makerworld.com.cn/zh/models/1"):
                self.assertNotIn("HTTP_PROXY", os.environ)
                self.assertNotIn("HTTPS_PROXY", os.environ)
                self.assertIn("makerworld.com.cn", os.environ.get("NO_PROXY", ""))
                self.assertIn("api.bambulab.cn", os.environ.get("no_proxy", ""))
            self.assertEqual(os.environ.get("HTTP_PROXY"), "http://existing.proxy")
            self.assertEqual(os.environ.get("HTTPS_PROXY"), "http://existing.proxy")
        finally:
            for key, value in previous.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value


if __name__ == "__main__":
    unittest.main()
