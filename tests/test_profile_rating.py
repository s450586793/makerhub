import unittest

from app.services.catalog import _normalize_instance_overview, _normalize_profile_details
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

    def test_normalizes_instance_time_from_profile_details(self):
        overview = _normalize_instance_overview({
            "name": "Timed profile",
            "profileDetails": {
                "printTimeSeconds": 38880,
            },
        })

        self.assertEqual(overview["time"], "10.8 h")

    def test_normalizes_legacy_filament_weights_for_catalog(self):
        details = _normalize_profile_details({
            "profileDetails": {
                "filaments": [
                    {
                        "material": "PLA",
                        "colorHex": "#FF9016",
                        "usedWeight": "69 g",
                        "isAMS": True,
                        "slotIndex": 1,
                    },
                    {
                        "material": "PETG",
                        "color_hex": "#FFFFFF",
                        "consumption": "12 g",
                        "trayIndex": 2,
                    },
                ],
            },
        })

        self.assertEqual(details["filament_weight"], 81)
        self.assertEqual(details["filament_weight_label"], "81 g")
        self.assertEqual(details["filaments"][0]["weight"], 69)
        self.assertEqual(details["filaments"][0]["weight_label"], "69 g")
        self.assertTrue(details["filaments"][0]["ams"])
        self.assertEqual(details["filaments"][0]["slot"], 1)
        self.assertEqual(details["filaments"][1]["weight"], 12)
        self.assertEqual(details["filaments"][1]["slot"], 2)


if __name__ == "__main__":
    unittest.main()
