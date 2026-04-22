import unittest

from app.services.catalog import _normalize_instance_overview
from app.services.profile_rating import normalize_profile_rating


class ProfileRatingTest(unittest.TestCase):
    def test_scales_ratio_rating_to_five_star_score(self):
        self.assertEqual(normalize_profile_rating(0.9619519703039768), 4.81)

    def test_clamps_rating_to_five(self):
        self.assertEqual(normalize_profile_rating(9.2), 5)

    def test_ignores_empty_or_zero_rating(self):
        self.assertIsNone(normalize_profile_rating(""))
        self.assertIsNone(normalize_profile_rating(0))

    def test_normalizes_instance_overview_rating(self):
        overview = _normalize_instance_overview({
            "name": "Test profile",
            "rating": 0.9619519703039768,
        })

        self.assertEqual(overview["rating"], 4.81)


if __name__ == "__main__":
    unittest.main()
