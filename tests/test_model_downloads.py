import asyncio
import json
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from fastapi import HTTPException

from app.api import config as config_api
from app.services import catalog


class ModelDownloadArchiveTest(unittest.TestCase):
    def test_download_all_archive_includes_model_assets_without_generated_packages(self):
        with tempfile.TemporaryDirectory() as tmp:
            archive_root = Path(tmp).resolve() / "archive"
            model_root = archive_root / "LOCAL_Demo"
            (model_root / "instances").mkdir(parents=True)
            (model_root / "images").mkdir()
            (model_root / "attachments").mkdir()
            (model_root / "file" / "manual").mkdir(parents=True)
            (model_root / "packages").mkdir()
            (model_root / "instances" / "body.stl").write_bytes(b"solid body\nendsolid body\n")
            (model_root / "images" / "cover.jpg").write_bytes(b"fake-jpg")
            (model_root / "attachments" / "guide.pdf").write_bytes(b"%PDF-1.7\n")
            (model_root / "file" / "manual" / "extra.txt").write_text("notes", encoding="utf-8")
            (model_root / "packages" / "Batch1.zip").write_bytes(b"nested-zip")
            (model_root / "meta.json").write_text(json.dumps({"title": "Demo"}, ensure_ascii=False), encoding="utf-8")

            cache_root = Path(tmp).resolve() / "state" / "downloads"
            with patch.object(config_api, "ARCHIVE_DIR", archive_root), \
                patch.object(config_api, "MODEL_DOWNLOAD_ALL_CACHE_DIR", cache_root):
                zip_path, download_name = config_api._create_model_download_all_archive("LOCAL_Demo")

            self.assertEqual(download_name, "Demo_所有文件.zip")
            with zipfile.ZipFile(zip_path) as archive:
                self.assertEqual(
                    sorted(archive.namelist()),
                    [
                        "attachments/guide.pdf",
                        "file/manual/extra.txt",
                        "images/cover.jpg",
                        "instances/body.stl",
                    ],
                )

    def test_bambu_studio_download_signature_resolves_only_3mf_instances(self):
        with tempfile.TemporaryDirectory() as tmp:
            archive_root = Path(tmp).resolve() / "archive"
            state_root = Path(tmp).resolve() / "state"
            model_root = archive_root / "LOCAL_Demo"
            (model_root / "instances").mkdir(parents=True)
            (model_root / "attachments").mkdir()
            (model_root / "instances" / "demo.3mf").write_bytes(b"3mf")
            (model_root / "instances" / "mesh.stl").write_bytes(b"solid mesh\nendsolid mesh\n")
            (model_root / "attachments" / "demo.3mf").write_bytes(b"wrong")
            (model_root / "meta.json").write_text(json.dumps({"title": "Demo"}, ensure_ascii=False), encoding="utf-8")

            state = {}
            with patch.object(config_api, "ARCHIVE_DIR", archive_root), \
                    patch.object(config_api, "STATE_DIR", state_root), \
                    patch.object(config_api, "load_database_json_state", side_effect=lambda key, default: dict(state.get(key) or default)), \
                    patch.object(config_api, "save_database_json_state", side_effect=lambda key, value: state.__setitem__(key, value) or value):
                expires_at = 4102444800
                signature = config_api._bambu_download_signature("LOCAL_Demo", "demo.3mf", expires_at)
                self.assertIn("bambu_studio_download_secret", state)
                self.assertEqual(
                    signature,
                    config_api._bambu_download_signature("LOCAL_Demo", "demo.3mf", expires_at),
                )
                target, safe_name = config_api._resolve_bambu_download_file("LOCAL_Demo", "demo.3mf")
                self.assertEqual(target, model_root / "instances" / "demo.3mf")
                self.assertEqual(safe_name, "demo.3mf")

                with self.assertRaises(ValueError):
                    config_api._resolve_bambu_download_file("LOCAL_Demo", "mesh.stl")
                with self.assertRaises(ValueError):
                    config_api._resolve_bambu_download_file("LOCAL_Demo", "../attachments/demo.3mf")

                with patch.object(config_api.time, "time", return_value=expires_at + 1):
                    with self.assertRaises(HTTPException) as context:
                        asyncio.run(
                            config_api.public_bambu_studio_download_file(
                                "LOCAL_Demo",
                                "demo.3mf",
                                expires=expires_at,
                                sig=signature,
                            )
                        )
                    self.assertEqual(context.exception.status_code, 403)

                with patch.object(config_api.time, "time", return_value=expires_at - 1):
                    response = asyncio.run(
                        config_api.public_bambu_studio_download_file(
                            "LOCAL_Demo",
                            "demo.3mf",
                            expires=expires_at,
                            sig=signature,
                        )
                    )
                    self.assertEqual(response.filename, "demo.3mf")


class CatalogAssetDownloadTest(unittest.TestCase):
    def test_instance_download_links_preserve_literal_filename_characters(self):
        names = (
            ("#0147 Dratini.3mf", "%230147%20Dratini.3mf"),
            ("宝可梦#92.3mf", "%E5%AE%9D%E5%8F%AF%E6%A2%A6%2392.3mf"),
            ("part?draft.3mf", "part%3Fdraft.3mf"),
            ("part%23literal.3mf", "part%2523literal.3mf"),
        )
        with tempfile.TemporaryDirectory() as tmp:
            archive_root = Path(tmp)
            model_root = archive_root / "LOCAL_Demo"
            (model_root / "instances").mkdir(parents=True)
            for name, encoded in names:
                with self.subTest(name=name):
                    (model_root / "instances" / name).write_bytes(b"3mf")
                    with patch.object(catalog, "ARCHIVE_DIR", archive_root):
                        instance = catalog._normalize_instances(
                            {"source": "local", "instances": [{"fileName": name}]}, model_root
                        )[0]
                    self.assertTrue(instance["file_available"])
                    self.assertEqual(instance["file_url"], f"/archive/LOCAL_Demo/instances/{encoded}")
                    self.assertEqual(instance["file_name"], name)
                    self.assertEqual(instance["file_size"], 3)

    def test_metadata_asset_links_do_not_choose_shorter_url_like_filenames(self):
        with tempfile.TemporaryDirectory() as tmp:
            archive_root = Path(tmp)
            model_root = archive_root / "LOCAL_Demo"
            (model_root / "attachments").mkdir(parents=True)
            (model_root / "attachments" / "guide.pdf").write_bytes(b"short")
            (model_root / "attachments" / "guide.pdf#v2").write_bytes(b"literal")
            meta = {"attachments": [
                {"localName": "attachments/guide.pdf#v2"},
                {"relPath": "attachments/guide.pdf?missing"},
            ]}
            with patch.object(catalog, "ARCHIVE_DIR", archive_root):
                attachments = catalog._normalize_attachments(meta, model_root)
            self.assertEqual(attachments[0]["url"], "/archive/LOCAL_Demo/attachments/guide.pdf%23v2")
            self.assertEqual(attachments[1]["url"], "/archive/LOCAL_Demo/attachments/guide.pdf%3Fmissing")

    def test_missing_literal_instance_does_not_match_existing_shorter_filename(self):
        with tempfile.TemporaryDirectory() as tmp:
            archive_root = Path(tmp)
            model_root = archive_root / "LOCAL_Demo"
            (model_root / "instances").mkdir(parents=True)
            (model_root / "instances" / "part.3mf").write_bytes(b"3mf")
            with patch.object(catalog, "ARCHIVE_DIR", archive_root):
                self.assertIsNone(catalog._existing_local_asset_url(model_root, "instances/part.3mf#missing"))
                self.assertIsNone(catalog._existing_local_asset_url(model_root, "instances/part.3mf?missing"))

    def test_raw_image_and_group_download_refs_preserve_literal_characters(self):
        with tempfile.TemporaryDirectory() as tmp:
            archive_root = Path(tmp)
            model_root = archive_root / "LOCAL_Demo"
            (model_root / "images").mkdir(parents=True)
            (model_root / "packages").mkdir()
            (model_root / "images" / "cover#1?.png").write_bytes(b"png")
            (model_root / "packages" / "batch#1?.zip").write_bytes(b"zip")
            with patch.object(catalog, "ARCHIVE_DIR", archive_root):
                image_url = catalog._asset_url_from_item(model_root, "images/cover#1?.png")
                groups = catalog._normalize_local_import_groups({"localImport": {"groups": [
                    {"downloadLocal": "packages/batch#1?.zip"},
                ]}}, model_root)
            self.assertEqual(image_url, "/archive/LOCAL_Demo/images/cover%231%3F.png")
            self.assertEqual(groups[0]["download_url"], "/archive/LOCAL_Demo/packages/batch%231%3F.zip")

    def test_summary_urls_keep_existing_query_fragment_and_encoding_semantics(self):
        with tempfile.TemporaryDirectory() as tmp:
            archive_root = Path(tmp)
            model_root = archive_root / "LOCAL_Demo"
            (model_root / "images").mkdir(parents=True)
            (model_root / "images" / "cover.png").write_bytes(b"png")
            (model_root / "images" / "cover.png?v=1#page").write_bytes(b"literal")
            with patch.object(catalog, "ARCHIVE_DIR", archive_root):
                summary = catalog._rewrite_summary_html(model_root,
                    '<img src="images/cover.png?v=1#page">'
                    '<img src="https://example.com/cover.png?v=1#page">'
                    '<a href="/archive/LOCAL_Demo/images/%23cover%3F.png?v=1#page">download</a>'
                )
            self.assertIn('src="/archive/LOCAL_Demo/images/cover.png"', summary)
            self.assertIn('src="https://example.com/cover.png?v=1#page"', summary)
            self.assertIn('href="/archive/LOCAL_Demo/images/%23cover%3F.png?v=1#page"', summary)

    def test_shared_and_bare_metadata_paths_keep_existing_lookup_locations(self):
        with tempfile.TemporaryDirectory() as tmp:
            archive_root = Path(tmp)
            model_root = archive_root / "LOCAL_Demo"
            (model_root / "images").mkdir(parents=True)
            (archive_root / "_shared" / "avatars").mkdir(parents=True)
            (model_root / "images" / "cover#1.png").write_bytes(b"cover")
            (archive_root / "_shared" / "avatars" / "face#1.png").write_bytes(b"avatar")
            with patch.object(catalog, "ARCHIVE_DIR", archive_root):
                self.assertEqual(catalog._asset_url_from_item(model_root, {"fileName": "cover#1.png"}),
                                 "/archive/LOCAL_Demo/images/cover%231.png")
                self.assertEqual(catalog._asset_url_from_item(model_root, {"avatarRelPath": "_shared/avatars/face#1.png"}),
                                 "/archive/_shared/avatars/face%231.png")


if __name__ == "__main__":
    unittest.main()
