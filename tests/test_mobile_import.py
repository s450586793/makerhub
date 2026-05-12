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
from app.schemas.models import AppConfig, MobileImportConfig


class MobileImportTokenTest(unittest.TestCase):
    def _request(self, token: str):
        return SimpleNamespace(
            headers={"Authorization": f"Bearer {token}"} if token else {},
            query_params={},
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
