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


if __name__ == "__main__":
    unittest.main()
