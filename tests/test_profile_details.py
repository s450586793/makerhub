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

    def test_keeps_scanning_nested_filaments_when_shallow_list_has_no_weights(self):
        inst = {
            "filaments": [
                {"material": "PLA", "color": "#FF9016"},
                {"material": "PETG", "color": "#FFFFFF"},
            ],
            "extension": {
                "modelInfo": {
                    "consumables": {
                        "trayList": [
                            {
                                "trayInfo": {
                                    "materialName": "PLA",
                                    "displayColor": "#FF9016",
                                    "usedWeightG": 70.5,
                                }
                            },
                            {
                                "trayInfo": {
                                    "materialName": "PETG",
                                    "displayColor": "#FFFFFF",
                                    "consumptionG": "18.2 g",
                                }
                            },
                        ]
                    }
                }
            },
        }

        details = normalize_profile_details(inst, [])

        self.assertEqual(details["filamentWeight"], 88.7)
        self.assertEqual(len(details["filaments"]), 2)
        self.assertEqual(details["filaments"][0]["weight"], 70.5)
        self.assertEqual(details["filaments"][1]["weight"], 18.2)

    def test_uses_parallel_filament_weight_list_without_splitting_total(self):
        inst = {
            "filaments": [
                {"material": "PLA", "color": "#FF9016"},
                {"material": "PETG", "color": "#FFFFFF"},
            ],
            "filamentWeights": ["70.5 g", "18.2 g"],
            "filamentWeight": 100,
        }

        details = normalize_profile_details(inst, [])

        self.assertEqual(details["filamentWeight"], 100)
        self.assertEqual(details["filaments"][0]["weight"], 70.5)
        self.assertEqual(details["filaments"][1]["weight"], 18.2)

    def test_does_not_split_total_filament_weight_across_materials(self):
        inst = {
            "filaments": [
                {"material": "PLA", "color": "#FF9016"},
                {"material": "PETG", "color": "#FFFFFF"},
            ],
            "filamentWeight": 100,
        }

        details = normalize_profile_details(inst, [])

        self.assertEqual(details["filamentWeight"], 100)
        self.assertEqual(details["filaments"][0]["weight"], 0)
        self.assertEqual(details["filaments"][1]["weight"], 0)

    def test_plate_total_weight_is_not_used_as_material_weight(self):
        plates = [
            {
                "index": 1,
                "prediction": {"weight": 100},
                "filaments": [
                    {"material": "PLA", "color": "#FF9016"},
                    {"material": "PETG", "color": "#FFFFFF"},
                ],
            }
        ]
        inst = {"extension": {"modelInfo": {"plates": plates}}}

        details = normalize_profile_details(inst, plates)

        self.assertEqual(details["filamentWeight"], 100)
        self.assertEqual(details["filaments"][0]["weight"], 0)
        self.assertEqual(details["filaments"][1]["weight"], 0)

    def test_recovers_nested_print_time_seconds(self):
        details = normalize_profile_details({
            "prediction": {
                "printTimeSeconds": 38880,
            },
        }, [])

        self.assertEqual(details["printTimeSeconds"], 38880)

    def test_recovers_scalar_prediction_as_print_time_seconds(self):
        details = normalize_profile_details({
            "prediction": 20446,
        }, [])

        self.assertEqual(details["printTimeSeconds"], 20446)


if __name__ == "__main__":
    unittest.main()
