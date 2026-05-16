import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch

from app.services import local_organizer


class FakeTaskStore:
    def __init__(self, payload=None):
        self.payload = dict(payload or {"items": []})

    def load_organize_tasks(self):
        return dict(self.payload)

    def save_organize_tasks(self, payload):
        self.payload = dict(payload)
        return self.payload

    def upsert_organize_task(self, item, limit=50):
        target_id = item.get("id") or item.get("fingerprint") or item.get("source_path")
        items = []
        replaced = False
        for existing in list(self.payload.get("items") or []):
            current_id = existing.get("id") or existing.get("fingerprint") or existing.get("source_path")
            if current_id == target_id:
                items.append(item)
                replaced = True
            else:
                items.append(existing)
        if not replaced:
            items.insert(0, item)
        self.payload = {**self.payload, "items": items[:limit], "count": len(items)}
        return self.payload

    def _update_organize_tasks(self, updater):
        updated = updater(dict(self.payload))
        if updated is not None:
            self.payload = dict(updated)
        return self.payload


class LocalOrganizerTest(unittest.TestCase):
    def test_sync_candidate_queue_moves_terminal_duplicate_source(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            source_dir = root / "source"
            library_root = root / "archive"
            source_dir.mkdir()
            library_root.mkdir()
            candidate = source_dir / "wechat-upload.3mf"
            candidate.write_bytes(b"demo-3mf")

            task_store = FakeTaskStore()
            service = local_organizer.LocalOrganizerService(
                store=SimpleNamespace(),
                task_store=task_store,
            )
            fingerprint = service._fingerprint(candidate)
            task_store.payload = {
                "items": [
                    {
                        "id": local_organizer._task_id_from_fingerprint(fingerprint),
                        "file_name": candidate.name,
                        "source_path": candidate.as_posix(),
                        "status": "skipped",
                        "message": "该 3MF 与模型库现有配置重复，已跳过。",
                        "fingerprint": fingerprint,
                    }
                ]
            }

            with patch.object(local_organizer, "_append_organizer_log"):
                actionable = service._sync_candidate_queue(
                    candidates=[candidate],
                    source_dir=source_dir,
                    library_root=library_root,
                    move_files=True,
                )

            self.assertEqual(actionable, [])
            self.assertFalse(candidate.exists())
            moved_files = list((source_dir / "_duplicates").glob("wechat-upload*.3mf"))
            self.assertEqual(len(moved_files), 1)
            task_item = task_store.payload["items"][0]
            self.assertEqual(task_item["status"], "skipped")
            self.assertEqual(task_item["target_path"], moved_files[0].as_posix())

    def test_sync_candidate_queue_does_not_reuse_stale_terminal_source_path(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            source_dir = root / "source"
            library_root = root / "archive"
            source_dir.mkdir()
            library_root.mkdir()
            candidate = source_dir / "wechat-upload.3mf"
            candidate.write_bytes(b"new-content")

            task_store = FakeTaskStore(
                {
                    "items": [
                        {
                            "id": "old-task",
                            "file_name": candidate.name,
                            "source_path": candidate.as_posix(),
                            "status": "skipped",
                            "message": "旧文件已跳过。",
                            "fingerprint": "old-fingerprint",
                        }
                    ]
                }
            )
            service = local_organizer.LocalOrganizerService(
                store=SimpleNamespace(),
                task_store=task_store,
            )

            with patch.object(local_organizer, "_append_organizer_log"):
                actionable = service._sync_candidate_queue(
                    candidates=[candidate],
                    source_dir=source_dir,
                    library_root=library_root,
                    move_files=True,
                )

            self.assertEqual(actionable, [candidate])
            self.assertTrue(candidate.exists())
            task_item = task_store.payload["items"][0]
            self.assertEqual(task_item["status"], "queued")
            self.assertEqual(task_item["source_path"], candidate.as_posix())
            self.assertNotEqual(task_item["fingerprint"], "old-fingerprint")

    def test_shortcut_fallback_name_uses_3mf_metadata_title(self):
        with TemporaryDirectory() as tmp:
            library_root = Path(tmp) / "archive"
            library_root.mkdir()
            source_path = Path(tmp) / "wechat-upload.3mf"
            source_path.write_bytes(b"demo-3mf")

            service = local_organizer.LocalOrganizerService(
                store=SimpleNamespace(),
                task_store=FakeTaskStore(),
            )
            title = "EVA 新世纪福音战士 明日香四色半分件/单色打印/双色x2d多版本"
            analysis = {
                "source_title": "wechat-upload",
                "model_title": title,
                "profile_title": title,
            }

            self.assertEqual(service._display_title_for_analysis(source_path, analysis), title)
            self.assertEqual(
                service._target_filename_for_analysis(source_path, analysis),
                "EVA 新世纪福音战士 明日香四色半分件_单色打印_双色x2d多版本.3mf",
            )

            model_root = service._prepare_model_root(
                library_root,
                source_path,
                existing={},
                matched_model=None,
                title=service._display_title_for_analysis(source_path, analysis),
            )

            self.assertEqual(
                model_root.name,
                "LOCAL_EVA 新世纪福音战士 明日香四色半分件_单色打印_双色x2d多版本",
            )

    def test_shortcut_fallback_title_ignores_placeholder_metadata(self):
        service = local_organizer.LocalOrganizerService(
            store=SimpleNamespace(),
            task_store=FakeTaskStore(),
        )
        source_path = Path("/tmp/wechat-upload.3mf")

        self.assertEqual(service._display_title_for_analysis(source_path, {}), "移动端导入")
        self.assertEqual(service._target_filename_for_analysis(source_path, {}), "wechat-upload.3mf")


if __name__ == "__main__":
    unittest.main()
