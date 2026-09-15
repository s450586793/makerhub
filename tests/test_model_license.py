import unittest

from bs4 import BeautifulSoup

from app.services.catalog import _normalize_model_license, _sanitize_summary_html


class ModelLicenseTest(unittest.TestCase):
    def test_preserves_archived_license_and_prefers_name(self):
        self.assertEqual(_normalize_model_license({"license": " CC BY-SA "}), "CC BY-SA")
        self.assertEqual(_normalize_model_license({"licenseName": "SDFL", "license": "code"}), "SDFL")

    def test_reads_structured_description(self):
        self.assertEqual(_normalize_model_license({"license": {"name": "Standard Digital File License"}}), "Standard Digital File License")
        self.assertEqual(_normalize_model_license({"licenseDescriptionInfo": {"description": "Custom terms"}}), "Custom terms")

    def test_does_not_invent_license_for_missing_or_unknown_values(self):
        for meta in ({}, {"license": None}, {"license": 7}, {"license": {"id": 4}}, {"license": []}):
            with self.subTest(meta=meta):
                self.assertEqual(_normalize_model_license(meta), "")

    def test_nested_removed_nodes_do_not_break_current_makerworld_description(self):
        soup = BeautifulSoup('<div><h2>Model</h2><svg><g><path d="M0 0"/></g><foreignObject><p>discard</p></foreignObject></svg><p onclick="bad()">Keep</p><a href="javascript:bad()">Link</a></div>', "html.parser")
        _sanitize_summary_html(soup)
        self.assertIsNone(soup.find("svg"))
        self.assertNotIn("discard", soup.get_text())
        self.assertEqual(soup.find("p").get_text(), "Keep")
        self.assertFalse(soup.find("p").has_attr("onclick"))
        self.assertFalse(soup.find("a").has_attr("href"))
