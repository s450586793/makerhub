import io
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from fastapi import UploadFile

from app.api import config as config_api
from app.services import business_logs, model_attachments


class ConfigAttachmentApiTest(unittest.IsolatedAsyncioTestCase):
    async def test_upload_persists_attachment_and_logs_its_category_without_overriding_log_category(self):
        request = SimpleNamespace(state=SimpleNamespace(auth_identity={"kind": "session"}))
        detail = {"model_dir": "sample-model", "title": "Sample"}
        content = b"assembly instructions"

        for category in ("assembly", "guide", "manual", "bom", "other"):
            with self.subTest(category=category), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                model_root = root / "sample-model"
                model_root.mkdir()
                (model_root / "meta.json").write_text(json.dumps({"title": "Sample"}), encoding="utf-8")
                upload = UploadFile(file=io.BytesIO(content), filename="guide.pdf")

                with (
                    patch.object(model_attachments, "ARCHIVE_DIR", root),
                    patch.object(config_api, "resolve_model_dir_from_short_key", return_value=""),
                    patch.object(config_api, "upsert_archive_snapshot_model") as upsert,
                    patch.object(config_api, "get_model_detail", return_value=detail),
                    patch.object(business_logs, "append_database_log_entry", return_value=True) as persist_log,
                ):
                    payload = await config_api.upload_model_attachment(
                        "sample-model", request, file=upload, name="Guide", category=category,
                    )

                self.assertTrue(payload["success"])
                self.assertEqual(payload["detail"], detail)
                attachment = payload["attachment"]
                self.assertEqual(attachment["category"], category)
                self.assertEqual((model_root / attachment["relPath"]).read_bytes(), content)
                self.assertEqual(model_attachments.load_manual_attachments(model_root), [attachment])
                self.assertTrue(upload.file.closed)
                upsert.assert_called_once_with("sample-model", "manual_attachment_uploaded", broadcast=False)
                persist_log.assert_called_once()
                self.assertEqual(persist_log.call_args.args[0], "business.log")
                entry = persist_log.call_args.args[1]
                self.assertEqual(entry["category"], "model")
                self.assertEqual(entry["event"], "attachment_uploaded")
                self.assertEqual(entry["attachment_category"], category)
                self.assertEqual(entry["attachment_id"], attachment["id"])
                self.assertEqual(entry["attachment_name"], "Guide")


if __name__ == "__main__":
    unittest.main()
