import unittest
from urllib.parse import unquote

from app.services.catalog import _sample_cover


class CatalogPlaceholderCoverTest(unittest.TestCase):
    def test_sample_cover_uses_local_svg_with_chinese_title(self):
        cover_url = _sample_cover("SWITCH卡带盒-塞尔达传说-大师剑")
        decoded = unquote(cover_url)

        self.assertTrue(cover_url.startswith("data:image/svg+xml"))
        self.assertNotIn("placehold.co", cover_url)
        self.assertNotIn("???", decoded)
        self.assertIn("SWITCH卡带盒", decoded)
        self.assertIn("PingFang SC", decoded)


if __name__ == "__main__":
    unittest.main()
