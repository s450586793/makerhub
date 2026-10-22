import tempfile
import unittest
from pathlib import Path

from app.services import local_model_preview


class LocalModelCoverSelectionTest(unittest.TestCase):
    def test_package_cover_prefers_finished_images_and_keeps_ties_stable(self):
        with tempfile.TemporaryDirectory() as tmp:
            images = ["images/instructions.png", "images/01.png", "images/finished.png",
                      "images/render_dimensions.png", "images/02.png"]
            selected = local_model_preview.ensure_package_preview_images(
                model_root=Path(tmp), model_files=[], image_paths=images, title="Test",
            )
            self.assertEqual(selected, ["images/finished.png", "images/01.png", "images/02.png",
                                        "images/instructions.png", "images/render_dimensions.png"])

    def test_preview_candidate_prefers_explicit_complete_model_over_parts(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "instances").mkdir()
            names = ["obj_10_bottom.stl", "Arm_R_Assembly.stl", "A1.stl", "complete_model.stl"]
            for name in names:
                (root / "instances" / name).write_bytes(b"model")
            meta = {"instances": [{"id": str(index), "fileName": name} for index, name in enumerate(names)]}
            self.assertEqual(local_model_preview.first_previewable_instance(meta, root), {
                "instance_key": "3", "file_name": "complete_model.stl", "file_kind": "STL",
            })

    def test_preview_candidate_does_not_infer_whole_model_from_arm_assembly(self):
        meta = {"instances": [{"id": "body", "fileName": "robot.stl"},
                              {"id": "arm", "fileName": "Arm_R_Assembly.stl"}]}
        self.assertEqual(local_model_preview.first_previewable_instance(meta)["instance_key"], "body")

    def test_current_preview_keeps_actual_source_binding_when_better_candidate_exists(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "instances").mkdir()
            (root / "images").mkdir()
            (root / "instances" / "arm.stl").write_bytes(b"arm")
            (root / "instances" / "complete.stl").write_bytes(b"complete")
            (root / "images" / "three_preview_arm.png").write_bytes(b"preview")
            meta = {"source": "local", "cover": "images/three_preview_arm.png",
                    "designImages": [{"relPath": "images/three_preview_arm.png", "generated": True,
                                      "kind": "generated_three_preview", "sourceFileName": "arm.stl",
                                      "sourceInstanceKey": "arm"}],
                    "instances": [{"id": "arm", "fileName": "arm.stl"},
                                  {"id": "complete", "fileName": "complete.stl"}],
                    "localImport": {"previewGenerator": "three", "previewVersion": 2,
                                    "previewStatus": "success", "previewNeedsGeneration": False,
                                    "previewFile": "images/three_preview_arm.png",
                                    "previewSourceFileName": "arm.stl", "previewSourceInstanceKey": "arm"}}
            state = local_model_preview.build_local_preview_state(meta, root)
            self.assertEqual(state["candidate"]["file_name"], "complete.stl")
            self.assertEqual(meta["localImport"]["previewSourceFileName"], "arm.stl")
            self.assertEqual(meta["localImport"]["previewSourceInstanceKey"], "arm")
            self.assertFalse(state["needs_generation"])


if __name__ == "__main__":
    unittest.main()
