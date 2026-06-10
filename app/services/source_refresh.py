from __future__ import annotations

import uuid
from pathlib import Path
from typing import Any, Optional

from app.core.store import JsonStore
from app.core.timezone import now_iso as china_now_iso
from app.services.remote_refresh import RemoteRefreshManager
from app.services.source_refresh_jobs import run_source_refresh_model_job
from app.services.task_state import TaskStateStore


def _source_refresh_run_id() -> str:
    return f"src-{uuid.uuid4().hex[:12]}"


def _source_refresh_task_id(run_id: str, model_dir: str, index: int) -> str:
    clean_model = str(model_dir or "").strip().strip("/") or f"item-{index}"
    return f"{run_id}:{clean_model}"


class SourceRefreshTaskManager(RemoteRefreshManager):
    """Phase-1 source refresh manager with independent runtime state.

    The per-model refresh implementation is still inherited from
    RemoteRefreshManager for compatibility. Queue/run state is split now so
    source refresh is no longer represented as archive queue work.
    """

    def __init__(
        self,
        store: Optional[JsonStore] = None,
        task_store: Optional[TaskStateStore] = None,
        archive_manager: Any = None,
        *,
        background_enabled: Optional[bool] = None,
    ) -> None:
        super().__init__(
            store=store,
            task_store=task_store,
            archive_manager=archive_manager,
            background_enabled=background_enabled,
        )
        self._current_source_run_id = ""

    def _service_busy_reason(self) -> str:
        organize_tasks = self.task_store.load_organize_tasks()
        for item in organize_tasks.get("items") or []:
            if str(item.get("status") or "").strip().lower() in {"pending", "queued", "running"}:
                return "local_organizer_busy"
        return ""

    def _source_run_payload(
        self,
        *,
        run_id: str,
        status: str,
        candidate_total: int,
        completed_total: int = 0,
        succeeded_total: int = 0,
        failed_total: int = 0,
        skipped_total: int = 0,
        timed_out_total: int = 0,
        manual: bool = False,
        started_at: str = "",
        finished_at: str = "",
        current_items: Optional[list[dict[str, Any]]] = None,
        manifest_path: Any = "",
        result_path: Any = "",
        message: str = "",
    ) -> dict[str, Any]:
        remaining_total = max(int(candidate_total or 0) - int(completed_total or 0), 0)
        now = china_now_iso()
        return {
            "run_id": run_id,
            "status": status,
            "manual": bool(manual),
            "created_at": started_at or now,
            "started_at": started_at or now,
            "updated_at": now,
            "finished_at": finished_at,
            "candidate_total": int(candidate_total or 0),
            "queued_total": remaining_total,
            "completed_total": int(completed_total or 0),
            "succeeded_total": int(succeeded_total or 0),
            "failed_total": int(failed_total or 0),
            "skipped_total": int(skipped_total or 0),
            "timed_out_total": int(timed_out_total or 0),
            "remaining_total": remaining_total,
            "current_items": list(current_items or [])[:8],
            "manifest_path": str(manifest_path or ""),
            "result_path": str(result_path or ""),
            "message": str(message or ""),
        }

    def _source_tasks_from_candidates(self, *, run_id: str, candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
        now = china_now_iso()
        tasks: list[dict[str, Any]] = []
        for index, item in enumerate(candidates, start=1):
            model_dir = str(item.get("model_dir") or "").strip().strip("/")
            tasks.append(
                {
                    "id": _source_refresh_task_id(run_id, model_dir, index),
                    "run_id": run_id,
                    "model_dir": model_dir,
                    "title": str(item.get("title") or model_dir or "未命名模型"),
                    "url": str(item.get("origin_url") or item.get("url") or ""),
                    "status": "queued",
                    "attempts": 0,
                    "created_at": now,
                    "updated_at": now,
                    "message": "等待源端刷新",
                    "metrics": {},
                }
            )
        return tasks

    def _publish_source_run_started(
        self,
        *,
        run_id: str,
        candidates: list[dict[str, Any]],
        active_run: dict[str, Any],
        manual: bool,
    ) -> None:
        now = china_now_iso()
        self._current_source_run_id = run_id
        self.task_store.save_source_refresh_queue(
            {
                "version": 1,
                "active": [],
                "queued": self._source_tasks_from_candidates(run_id=run_id, candidates=candidates),
                "recent_failures": self.task_store.load_source_refresh_queue().get("recent_failures") or [],
                "updated_at": now,
            }
        )
        self.task_store.patch_source_refresh_runs(
            active_run=self._source_run_payload(
                run_id=run_id,
                status="running",
                manual=manual,
                started_at=str(active_run.get("started_at") or now),
                candidate_total=int(active_run.get("candidate_total") or len(candidates)),
                completed_total=int(active_run.get("completed_total") or 0),
                manifest_path=active_run.get("manifest_path") or "",
                result_path=active_run.get("result_path") or "",
                message="源端刷新运行中。",
            ),
            last_attempt_at=now,
            last_defer_reason="",
        )

    def _publish_source_run_completed(
        self,
        *,
        active_run: dict[str, Any],
        records: list[dict[str, Any]],
        succeeded: int,
        failed: int,
        skipped: int,
        message: str,
    ) -> None:
        run_id = self._current_source_run_id or str(active_run.get("batch_id") or active_run.get("run_id") or _source_refresh_run_id())
        finished_at = china_now_iso()
        completed_total = len(records)
        candidate_total = int(active_run.get("candidate_total") or completed_total)
        terminal_failures = []
        if failed:
            for record in records:
                status = str(record.get("status") or "")
                if status not in {"failed", "error"}:
                    continue
                terminal_failures.append(
                    {
                        "id": _source_refresh_task_id(run_id, str(record.get("model_dir") or ""), len(terminal_failures) + 1),
                        "run_id": run_id,
                        "model_dir": str(record.get("model_dir") or ""),
                        "title": str(record.get("title") or ""),
                        "url": str(record.get("url") or ""),
                        "status": "failed",
                        "message": str(record.get("message") or ""),
                        "updated_at": finished_at,
                    }
                )
        self.task_store.save_source_refresh_queue(
            {
                "version": 1,
                "active": [],
                "queued": [],
                "recent_failures": terminal_failures + (self.task_store.load_source_refresh_queue().get("recent_failures") or []),
                "updated_at": finished_at,
            }
        )
        completed_run = self._source_run_payload(
            run_id=run_id,
            status="completed" if failed == 0 else "failed",
            manual=bool(active_run.get("manual")),
            started_at=str(active_run.get("started_at") or ""),
            finished_at=finished_at,
            candidate_total=candidate_total,
            completed_total=completed_total,
            succeeded_total=succeeded,
            failed_total=failed,
            skipped_total=skipped,
            manifest_path=active_run.get("manifest_path") or "",
            result_path=active_run.get("result_path") or "",
            message=message,
        )
        self.task_store.patch_source_refresh_runs(
            active_run={},
            last_completed_run=completed_run,
            last_interrupted_at="" if failed == 0 else finished_at,
            last_interrupted_reason="" if failed == 0 else message,
        )
        self._current_source_run_id = ""

    def _publish_source_run_completed_from_state(self, *, run_id: str) -> None:
        state = self.task_store.load_remote_refresh_state()
        finished_at = china_now_iso()
        succeeded = int(state.get("last_batch_succeeded") or 0)
        failed = int(state.get("last_batch_failed") or 0)
        skipped = int(state.get("last_batch_skipped") or 0)
        completed_total = succeeded + failed + skipped
        candidate_total = int(state.get("last_batch_total") or completed_total)
        self.task_store.save_source_refresh_queue(
            {
                "version": 1,
                "active": [],
                "queued": [],
                "recent_failures": self.task_store.load_source_refresh_queue().get("recent_failures") or [],
                "updated_at": finished_at,
            }
        )
        self.task_store.patch_source_refresh_runs(
            active_run={},
            last_completed_run=self._source_run_payload(
                run_id=run_id,
                status="completed" if failed == 0 else "failed",
                manual=False,
                started_at=str(state.get("last_run_at") or ""),
                finished_at=finished_at,
                candidate_total=candidate_total,
                completed_total=completed_total,
                succeeded_total=succeeded,
                failed_total=failed,
                skipped_total=skipped,
                manifest_path=(state.get("active_run") or {}).get("manifest_path") if isinstance(state.get("active_run"), dict) else "",
                result_path=(state.get("active_run") or {}).get("result_path") if isinstance(state.get("active_run"), dict) else "",
                message=str(state.get("last_message") or "源端刷新完成。"),
            ),
        )
        self._current_source_run_id = ""

    def _publish_source_run_interrupted(self, *, run_id: str, message: str) -> None:
        now = china_now_iso()
        queue = self.task_store.load_source_refresh_queue()
        self.task_store.save_source_refresh_queue(
            {
                "version": 1,
                "active": [],
                "queued": queue.get("queued") or [],
                "recent_failures": queue.get("recent_failures") or [],
                "updated_at": now,
            }
        )
        runs = self.task_store.load_source_refresh_runs()
        active_run = runs.get("active_run") if isinstance(runs.get("active_run"), dict) else {}
        if not active_run:
            active_run = self._source_run_payload(
                run_id=run_id,
                status="interrupted",
                candidate_total=int(queue.get("queued_count") or 0),
                message=message,
            )
        else:
            active_run = {**active_run, "status": "interrupted", "message": message, "updated_at": now}
        self.task_store.patch_source_refresh_runs(
            active_run=active_run,
            last_interrupted_at=now,
            last_interrupted_reason=message,
        )

    def _active_run_payload(self, **kwargs: Any) -> dict[str, Any]:
        active_run = super()._active_run_payload(**kwargs)
        if str(active_run.get("status") or "") == "running":
            run_id = self._current_source_run_id or _source_refresh_run_id()
            self._current_source_run_id = run_id
        return active_run

    def trigger_manual_refresh(self) -> dict[str, Any]:
        result = super().trigger_manual_refresh()
        if result.get("accepted"):
            state = result.get("state") if isinstance(result.get("state"), dict) else {}
            run_id = self._current_source_run_id or _source_refresh_run_id()
            self._current_source_run_id = run_id
            self.task_store.patch_source_refresh_runs(
                active_run=self._source_run_payload(
                    run_id=run_id,
                    status="running" if result.get("mode") != "resume" else "resuming",
                    manual=True,
                    candidate_total=int((state.get("active_run") or {}).get("candidate_total") or 0) if isinstance(state.get("active_run"), dict) else 0,
                    completed_total=int((state.get("active_run") or {}).get("completed_total") or 0) if isinstance(state.get("active_run"), dict) else 0,
                    message=str(result.get("message") or ""),
                ),
                last_attempt_at=china_now_iso(),
                last_defer_reason="",
            )
        return result

    def repair_source_refresh_state(self) -> dict[str, Any]:
        queue = self.task_store.load_source_refresh_queue()
        runs = self.task_store.load_source_refresh_runs()
        return {
            "summary": {
                "running_count": int(queue.get("running_count") or 0),
                "queued_count": int(queue.get("queued_count") or 0),
                "active_run": bool(runs.get("active_run")),
            },
            "queue": queue,
            "runs": runs,
        }

    def _finalize_batch_from_records(self, **kwargs: Any) -> None:
        super()._finalize_batch_from_records(**kwargs)
        active_run = kwargs.get("active_run") if isinstance(kwargs.get("active_run"), dict) else {}
        records = kwargs.get("records") if isinstance(kwargs.get("records"), list) else []
        succeeded = len([item for item in records if str(item.get("status") or "") in {"success", "source_deleted"}])
        failed = len([item for item in records if str(item.get("status") or "") in {"failed", "error"}])
        skipped = len([item for item in records if str(item.get("status") or "") == "skipped"])
        self._publish_source_run_completed(
            active_run=active_run,
            records=records,
            succeeded=succeeded,
            failed=failed,
            skipped=skipped,
            message="源端刷新完成。",
        )

    def _run_batch(self, config, **kwargs: Any) -> None:
        if not kwargs.get("resume_active_run"):
            candidates, stats = self._pick_candidates()
            run_id = self._current_source_run_id or _source_refresh_run_id()
            self._current_source_run_id = run_id
            manifest_path = Path()
            result_path = Path()
            active_run = {
                "batch_id": run_id,
                "started_at": china_now_iso(),
                "candidate_total": len(candidates),
                "completed_total": 0,
                "manifest_path": str(manifest_path),
                "result_path": str(result_path),
                "manual": bool(self.task_store.load_remote_refresh_state().get("manual_requested_at")),
            }
            self._publish_source_run_started(
                run_id=run_id,
                candidates=candidates,
                active_run=active_run,
                manual=bool(active_run.get("manual")),
            )
            original_pick_candidates = self._pick_candidates
            self._pick_candidates = lambda: (candidates, stats)
            try:
                super()._run_batch(config, **kwargs)
            except Exception as exc:
                self._publish_source_run_interrupted(run_id=run_id, message=str(exc))
                raise
            else:
                self._publish_source_run_completed_from_state(run_id=run_id)
            finally:
                self._pick_candidates = original_pick_candidates
            return
        super()._run_batch(config, **kwargs)
