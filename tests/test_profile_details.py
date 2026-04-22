import unittest

from app.services.legacy_archiver import normalize_profile_details


class ProfileDetailsTest(unittest.TestCase):
    def test_recovers_nested_filaments_from_instance_payload(self):
        inst = {
            "name": "Nested filament profile",
            "extension": {
                "modelInfo": {
                    "consumables": {
                        "trayList": [
                            {
                                "trayInfo": {
                                    "materialName": "PLA",
                                    "displayColor": "#FF9016",
                                    "usedWeight": "69 g",
                                    "isAMS": True,
                                    "slotIndex": 1,
                                }
                            },
                            {
                                "trayInfo": {
                                    "materialName": "PLA",
                                    "displayColor": "#FFFFFF",
                                    "usedWeight": "12 g",
                                    "isAMS": True,
                                    "slotIndex": 2,
                                }
                            },
                        ]
                    }
                }
            }
        }

        details = normalize_profile_details(inst, [])

        self.assertTrue(details["needAms"])
        self.assertEqual(details["filamentWeight"], 81)
        self.assertEqual(len(details["filaments"]), 2)
        self.assertEqual(details["filaments"][0]["material"], "PLA")
        self.assertEqual(details["filaments"][0]["color"], "#FF9016")
        self.assertEqual(details["filaments"][0]["weight"], 69)
        self.assertEqual(details["filaments"][0]["slot"], 1)
        self.assertEqual(details["filaments"][1]["color"], "#FFFFFF")
        self.assertEqual(details["filaments"][1]["weight"], 12)


if __name__ == "__main__":
    unittest.main()
