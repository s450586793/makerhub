from __future__ import annotations

import hashlib
from typing import Any

from app.core.timezone import now_iso as china_now_iso
from app.services.runtime_engine import store
from app.services.runtime_engine.contracts import normalize_run_type


def _stable_id(prefix: str, *parts: Any) -> str:
    digest = hashlib.sha1("::".join(str(part or "") for part in parts).encode("utf-8")).hexdigest()[:24]
    return f"{prefix}-{digest}"


class RuntimeEngine:
    def __init__(self, *, adapters: dict[str, Any] | None = None, batch_size: int = 50) -> None:
        self.adapters = adapters or {}
        self.batch_size = max(1, min(int(batch_size or 50), 500))

    def submit_run(self, run_type: str, context: dict[str, Any] | None = None) -> dict[str, Any]:
        clean_type = normalize_run_type(run_type)
        clean_context = dict(context or {})
        run_id = str(
            clean_context.get("run_id")
            or _stable_id("run", clean_type, clean_context.get("source_url"), china_now_iso())
        )
        adapter = self.adapters.get(clean_type)
        if adapter is None:
            run = store.upsert_run(
                {
                    "run_id": run_id,
                    "type": clean_type,
                    "status": "blocked",
                    "message": f"Runtime adapter not registered: {clean_type}",
                    "created_at": china_now_iso(),
                },
                event_type="runtime.run.blocked",
            )
            self.refresh_snapshots()
            return run

        run = store.upsert_run(
            {
                "run_id": run_id,
                "type": clean_type,
                "source_url": clean_context.get("source_url") or clean_context.get("url") or "",
                "source_id": clean_context.get("source_id") or "",
                "platform": clean_context.get("platform") or "",
                "status": "discovering",
                "created_at": china_now_iso(),
                "started_at": china_now_iso(),
                "message": "正在发现候选项。",
            },
            event_type="runtime.run.started",
        )
        candidates = adapter.discover({**clean_context, "run_id": run_id, "type": clean_type})
        batch_plans = adapter.plan(candidates, {"batch_size": self.batch_size})
        for index, plan in enumerate(batch_plans):
            items = list(plan.get("items") or [])
            batch_id = _stable_id("batch", run_id, index)
            store.save_batch_items(batch_id, items)
            store.upsert_batch(
                {
                    "batch_id": batch_id,
                    "run_id": run_id,
                    "type": clean_type,
                    "status": "queued",
                    "offset": plan.get("offset") or index * self.batch_size,
                    "limit": plan.get("limit") or self.batch_size,
                    "total": len(items),
                    "message": "等待执行。",
                    "created_at": china_now_iso(),
                }
            )
        run = store.upsert_run(
            {
                **run,
                "status": "planned",
                "total": len(candidates),
                "message": f"已规划 {len(batch_plans)} 个批次。",
                "updated_at": china_now_iso(),
            }
        )
        self.refresh_snapshots()
        return run

    def refresh_snapshots(self) -> dict[str, Any]:
        state = store.load_runtime_state()
        runs = state["runs"]["items"]
        batches = state["batches"]["items"]
        failures = state["failures"]["items"]
        active_statuses = {"queued", "discovering", "planned", "running", "paused", "blocked", "interrupted"}
        active_runs = [run for run in runs if run.get("status") in active_statuses][:20]
        active_batches = [batch for batch in batches if batch.get("status") in active_statuses][:50]
        task_snapshot = {
            "runs": active_runs,
            "batches": active_batches,
            "failures": failures[:100],
        }
        dashboard_snapshot = {
            "active_runs": active_runs[:8],
            "active_batches": active_batches[:8],
            "summary": {
                "active_runs": len(active_runs),
                "active_batches": len(active_batches),
                "failures": len(failures),
            },
        }
        store.save_snapshot("tasks", task_snapshot)
        store.save_snapshot("dashboard", dashboard_snapshot)
        return {"tasks": task_snapshot, "dashboard": dashboard_snapshot}

    def execute_next_batch(self) -> dict[str, Any]:
        batches = store.load_batches()["items"]
        target = next((batch for batch in batches if batch.get("status") == "queued"), None)
        if not target:
            return {"executed": False, "message": "没有等待执行的批次。"}
        adapter = self.adapters.get(target.get("type"))
        if adapter is None:
            store.upsert_batch(
                {**target, "status": "blocked", "message": "运行适配器未注册。"},
                event_type="runtime.run.blocked",
            )
            self.refresh_snapshots()
            return {"executed": False, "message": "运行适配器未注册。"}

        batch_id = str(target.get("batch_id") or "")
        run_id = str(target.get("run_id") or "")
        store.upsert_batch({**target, "status": "running", "started_at": china_now_iso()}, event_type="runtime.batch.progress")
        items = store.load_batch_items(batch_id)
        completed = 0
        failed = 0
        for item in items:
            try:
                context = {"run_id": run_id, "batch_id": batch_id}
                result = adapter.execute_item(item, context)
                adapter.commit_success(result, context)
                completed += 1
            except Exception as exc:
                failure = adapter.classify_failure(exc)
                store.append_failure({**failure, "run_id": run_id, "batch_id": batch_id, "type": target.get("type")})
                failed += 1

        status = "completed" if failed == 0 or completed > 0 else "failed"
        completed_batch = {
            **target,
            "status": status,
            "completed": completed,
            "failed": failed,
            "completed_at": china_now_iso(),
            "message": f"批次完成：成功 {completed}，失败 {failed}。",
        }
        store.upsert_batch(
            completed_batch,
            event_type="runtime.batch.completed",
        )
        store.delete_batch_items(batch_id)
        updated_batches = [completed_batch if item.get("batch_id") == batch_id else item for item in batches]
        self._update_run_totals(run_id, batches=updated_batches)
        self.refresh_snapshots()
        return {"executed": True, "completed": completed, "failed": failed}

    def _update_run_totals(
        self,
        run_id: str,
        *,
        runs: list[dict[str, Any]] | None = None,
        batches: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        all_runs = runs if runs is not None else store.load_runs()["items"]
        all_batches = batches if batches is not None else store.load_batches()["items"]
        batches = [item for item in all_batches if item.get("run_id") == run_id]
        run = next((item for item in all_runs if item.get("run_id") == run_id), None)
        if not run:
            return {}
        completed = sum(int(item.get("completed") or 0) for item in batches)
        failed = sum(int(item.get("failed") or 0) for item in batches)
        total = sum(int(item.get("total") or 0) for item in batches) or int(run.get("total") or 0)
        active = [item for item in batches if item.get("status") in {"queued", "running", "paused", "blocked", "interrupted"}]
        status = "completed" if not active and failed == 0 else "failed" if not active else run.get("status", "running")
        event_type = "runtime.run.completed" if status == "completed" else ""
        return store.upsert_run(
            {
                **run,
                "status": status,
                "total": total,
                "completed": completed,
                "failed": failed,
                "updated_at": china_now_iso(),
                "completed_at": china_now_iso() if status in {"completed", "failed"} else run.get("completed_at", ""),
            },
            event_type=event_type,
        )

    def repair(self) -> dict[str, Any]:
        state = store.load_runtime_state()
        repaired_batches: list[dict[str, Any]] = []
        for batch in state["batches"]["items"]:
            if batch.get("status") == "interrupted":
                batch = {
                    **batch,
                    "status": "queued",
                    "lease_owner": "",
                    "lease_expires_at": "",
                    "message": "已恢复为排队。",
                }
                store.upsert_batch(batch)
            repaired_batches.append(batch)
        for run in state["runs"]["items"]:
            self._update_run_totals(run["run_id"], runs=state["runs"]["items"], batches=repaired_batches)
        snapshots = self.refresh_snapshots()
        return {"success": True, "message": "运行核心状态已修复。", "snapshots": snapshots}

    def set_run_status(self, run_id: str, status: str) -> dict[str, Any]:
        runs = store.load_runs()["items"]
        run = next((item for item in runs if item.get("run_id") == run_id), None)
        if not run:
            return {"success": False, "run_id": run_id, "status": "not_found", "message": "运行不存在。"}
        updated = store.upsert_run({**run, "status": status, "updated_at": china_now_iso()})
        self.refresh_snapshots()
        return updated

    def retry_failures(self, payload: dict[str, Any]) -> dict[str, Any]:
        failure_ids = {str(item) for item in payload.get("failure_ids") or []}
        failures = store.load_failures()["items"]
        selected = [item for item in failures if not failure_ids or item.get("failure_id") in failure_ids]
        context = {
            "failure_ids": [item.get("failure_id") for item in selected],
            "platform": payload.get("platform") or "",
            "status": payload.get("status") or "",
        }
        return self.submit_run("missing_3mf_retry", context)
