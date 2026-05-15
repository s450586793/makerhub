import tempfile
import unittest
import zipfile
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from fastapi import HTTPException
from fastapi import UploadFile

from app.api import config as config_api
from app.core.security import hash_api_token
from app.core.store import JsonStore
from app.schemas.models import ApiTokenRecord, AppConfig, MobileImportConfig


class MobileImportTokenTest(unittest.TestCase):
    def _request(self, token: str):
        return SimpleNamespace(
            headers={"Authorization": f"Bearer {token}"} if token else {},
            query_params={},
        )

    def _filename_request(self, *, query_filename="", makerhub_header="", header_filename="", content_disposition=""):
        headers = {}
        if makerhub_header:
            headers["X-MakerHub-Filename"] = makerhub_header
        if header_filename:
            headers["X-Filename"] = header_filename
        if content_disposition:
            headers["content-disposition"] = content_disposition
        return SimpleNamespace(
            headers=headers,
            query_params={"filename": query_filename} if query_filename else {},
        )

    def test_mobile_import_token_allows_only_enabled_matching_token(self):
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "config.json"
            store = JsonStore(path=config_path)
            raw_token = "mhi_test_token"
            config = AppConfig()
            config.mobile_import = MobileImportConfig(
                enabled=True,
                token_prefix=raw_token[:12],
                token_hash=hash_api_token(raw_token),
                created_at="2026-05-12T10:00:00+08:00",
            )
            store.save(config)

            with patch.object(config_api, "store", store):
                config_api._require_mobile_import_token(self._request(raw_token))
                saved = store.load()
                self.assertTrue(saved.mobile_import.last_used_at)

                with self.assertRaises(HTTPException):
                    config_api._require_mobile_import_token(self._request("wrong"))

                saved.mobile_import.enabled = False
                store.save(saved)
                with self.assertRaises(HTTPException):
                    config_api._require_mobile_import_token(self._request(raw_token))

    def test_mobile_import_accepts_unified_token_with_permission(self):
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "config.json"
            store = JsonStore(path=config_path)
            raw_token = "mhi_unified_token"
            config = AppConfig()
            config.api_tokens = [
                ApiTokenRecord(
                    id="token-1",
                    name="iPhone",
                    token_prefix=raw_token[:12],
                    token_hash=hash_api_token(raw_token),
                    token_value=raw_token,
                    permissions=["mobile_import"],
                    created_at="2026-05-12T10:00:00+08:00",
                )
            ]
            store.save(config)

            with patch.object(config_api, "store", store), \
                    patch.object(config_api.auth_manager, "store", store):
                config_api._require_mobile_import_token(self._request(raw_token))
                saved = store.load()
                self.assertTrue(saved.api_tokens[0].last_used_at)

                saved.api_tokens[0].permissions = ["archive_write"]
                saved.api_tokens[0].last_used_at = ""
                store.save(saved)
                with self.assertRaises(HTTPException):
                    config_api._require_mobile_import_token(self._request(raw_token))

                saved = store.load()
                saved.api_tokens[0].permissions = ["mobile_import"]
                saved.api_tokens[0].disabled = True
                store.save(saved)
                with self.assertRaises(HTTPException):
                    config_api._require_mobile_import_token(self._request(raw_token))

    def test_mobile_raw_upload_infers_suffix_when_shortcut_omits_filename(self):
        stl_body = b"solid makerhub\nendsolid makerhub\n"
        upload, filename = config_api._mobile_raw_upload_file(stl_body, "wechat-upload")
        self.assertEqual(filename, "wechat-upload.stl")
        self.assertEqual(upload.filename, filename)

        zip_buffer = BytesIO()
        with zipfile.ZipFile(zip_buffer, "w") as archive:
            archive.writestr("3D/3dmodel.model", "<model />")
        upload, filename = config_api._mobile_raw_upload_file(zip_buffer.getvalue(), "wechat-upload")
        self.assertEqual(filename, "wechat-upload.3mf")
        self.assertEqual(upload.filename, filename)

        upload, filename = config_api._mobile_raw_upload_file(b"Rar!\x1a\x07\x01\x00", "wechat-upload")
        self.assertEqual(filename, "wechat-upload.rar")
        self.assertEqual(upload.filename, filename)

    def test_mobile_import_display_name_hides_shortcut_fallback(self):
        self.assertEqual(config_api._mobile_import_clean_name(["wechat-upload"]), "移动端导入")
        self.assertEqual(config_api._mobile_import_clean_name(["wechat-upload.stl"]), "移动端导入")
        self.assertEqual(config_api._mobile_import_clean_name(["wechat-upload.3mf"]), "移动端导入")
        self.assertEqual(config_api._mobile_import_clean_name(["demo.stl"]), "demo.stl")

    def test_mobile_import_filename_prefers_real_query_name(self):
        filename, source = config_api._mobile_import_request_filename(
            self._filename_request(query_filename="米老鼠.3mf", makerhub_header="wechat-upload")
        )
        self.assertEqual(filename, "米老鼠.3mf")
        self.assertEqual(source, "query")

        filename, source = config_api._mobile_import_request_filename(
            self._filename_request(query_filename="wechat-upload", makerhub_header="Garfield.3mf")
        )
        self.assertEqual(filename, "Garfield.3mf")
        self.assertEqual(source, "x-makerhub-filename")

        filename, source = config_api._mobile_import_request_filename(
            self._filename_request(content_disposition="attachment; filename*=UTF-8''%E7%B1%B3%E8%80%81%E9%BC%A0.3mf")
        )
        self.assertEqual(filename, "米老鼠.3mf")
        self.assertEqual(source, "content-disposition")

    def test_mobile_background_marks_upload_task_and_triggers_package(self):
        upload = UploadFile(file=BytesIO(b"solid makerhub\nendsolid makerhub\n"), filename="demo.stl")
        result = {
            "success": True,
            "mode": "package",
            "queued": True,
            "trigger_organizer": False,
            "task_id": "pkg-1",
            "uploaded": [{"file_name": "demo.stl", "status": "queued"}],
        }

        with patch.object(config_api, "upload_local_import_files", return_value=result), \
            patch.object(config_api, "BACKGROUND_TASKS_ENABLED", True), \
            patch.object(config_api.local_organizer, "run_once") as run_once, \
            patch.object(config_api.task_state_store, "upsert_organize_task") as upsert_task:
            config_api._run_mobile_import_background([upload], ["demo.stl"], "mobile-1")

        run_once.assert_called_once()
        payload = upsert_task.call_args_list[0].args[0]
        self.assertEqual(payload["id"], "mobile-1")
        self.assertEqual(payload["kind"], "mobile_import_upload")
        self.assertEqual(payload["status"], "success")
        self.assertEqual(payload["progress"], 100)
        self.assertEqual(payload["package_source"], "local-package:pkg-1")


if __name__ == "__main__":
    unittest.main()
