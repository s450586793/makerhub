import tempfile
import unittest
from pathlib import Path

from app.services.legacy_archiver import collect_design_images


class _BinaryResponse:
    status_code = 200
    text = ""

    def __init__(self, content: bytes):
        self._content = content

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def raise_for_status(self):
        return None

    def iter_content(self, chunk_size=65536):
        yield self._content


class _CountingSession:
    def __init__(self):
        self.calls = []

    def get(self, url, timeout=None, stream=False):
        self.calls.append(url)
        return _BinaryResponse(f"downloaded:{url}".encode("utf-8"))


class AssetSyncTest(unittest.TestCase):
    def test_design_image_reuses_existing_file_when_remote_url_is_unchanged(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            images_dir = Path(temp_dir) / "MW_1_Test" / "images"
            images_dir.mkdir(parents=True)
            existing_file = images_dir / "design_01.jpg"
            existing_file.write_bytes(b"existing")
            session = _CountingSession()

            images, _cover = collect_design_images(
                {"images": [{"url": "https://cdn.example.com/design-a.jpg"}]},
                session,
                images_dir,
                "MW_1_Test",
                download_assets=True,
                existing_images=[
                    {
                        "index": 1,
                        "originalUrl": "https://cdn.example.com/design-a.jpg",
                        "relPath": "images/design_01.jpg",
                        "fileName": "design_01.jpg",
                    }
                ],
            )

            self.assertEqual(session.calls, [])
            self.assertEqual(existing_file.read_bytes(), b"existing")
            self.assertEqual(images[0]["relPath"], "images/design_01.jpg")

    def test_design_image_overwrites_same_slot_when_remote_url_changes(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            images_dir = Path(temp_dir) / "MW_1_Test" / "images"
            images_dir.mkdir(parents=True)
            existing_file = images_dir / "design_01.jpg"
            existing_file.write_bytes(b"existing")
            session = _CountingSession()

            images, _cover = collect_design_images(
                {"images": [{"url": "https://cdn.example.com/design-b.jpg"}]},
                session,
                images_dir,
                "MW_1_Test",
                download_assets=True,
                existing_images=[
                    {
                        "index": 1,
                        "originalUrl": "https://cdn.example.com/design-a.jpg",
                        "relPath": "images/design_01.jpg",
                        "fileName": "design_01.jpg",
                    }
                ],
            )

            self.assertEqual(session.calls, ["https://cdn.example.com/design-b.jpg"])
            self.assertEqual(existing_file.read_bytes(), b"downloaded:https://cdn.example.com/design-b.jpg")
            self.assertEqual(images[0]["relPath"], "images/design_01.jpg")

    def test_design_image_does_not_reuse_old_file_ref_for_changed_url_during_metadata_pass(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            images_dir = Path(temp_dir) / "MW_1_Test" / "images"
            images_dir.mkdir(parents=True)
            (images_dir / "design_01.jpg").write_bytes(b"existing")
            session = _CountingSession()

            images, _cover = collect_design_images(
                {"images": [{"url": "https://cdn.example.com/design-b.jpg"}]},
                session,
                images_dir,
                "MW_1_Test",
                download_assets=False,
                existing_images=[
                    {
                        "index": 1,
                        "originalUrl": "https://cdn.example.com/design-a.jpg",
                        "relPath": "images/design_01.jpg",
                        "fileName": "design_01.jpg",
                    }
                ],
            )

            self.assertEqual(session.calls, [])
            self.assertEqual(images[0]["originalUrl"], "https://cdn.example.com/design-b.jpg")
            self.assertEqual(images[0]["relPath"], "")
            self.assertEqual(images[0]["fileName"], "")


if __name__ == "__main__":
    unittest.main()
