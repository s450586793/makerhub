import unittest

from app.api.config import _parse_github_changelog


class GithubChangelogTest(unittest.TestCase):
    def test_parse_heading_version(self):
        markdown = """
## 更新记录

### 2026-05-15 · v0.6.99
- 修复更新状态。

### 2026-05-15 · v0.6.98
- 增加 Token 页。
"""

        entries = _parse_github_changelog(markdown, limit=2)

        self.assertEqual(entries[0]["date"], "2026-05-15")
        self.assertEqual(entries[0]["version"], "0.6.99")
        self.assertEqual(entries[0]["items"], ["修复更新状态。"])
        self.assertEqual(entries[1]["date"], "2026-05-15")
        self.assertEqual(entries[1]["version"], "0.6.98")

    def test_parse_stops_before_collapsed_history(self):
        markdown = """
## 更新记录

### 2026-05-28 · v0.8.0
- 新增浏览器验证。

### 2026-05-27 · v0.7.11
- 修复缺失 3MF 队列。

### 2026-05-27 · v0.7.10
- 修复重试额度。

<details>
<summary>历史更新记录</summary>

### 2026-05-27 · v0.7.9
- 修复源端刷新。
</details>
"""

        entries = _parse_github_changelog(markdown)

        self.assertEqual([entry["version"] for entry in entries], ["0.8.0", "0.7.11", "0.7.10"])


if __name__ == "__main__":
    unittest.main()
