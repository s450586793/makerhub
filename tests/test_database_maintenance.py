from unittest.mock import patch

from app.services import database_maintenance


def test_delete_expired_batch_uses_strict_cutoff_and_limit():
    calls = []

    class FakeResult:
        rowcount = 37

    class FakeConnection:
        def execute(self, sql, params=None):
            calls.append((sql, params))
            return FakeResult()

    class FakeContext:
        def __enter__(self):
            return FakeConnection()

        def __exit__(self, exc_type, exc, tb):
            return False

    with patch.object(database_maintenance, "database_connection", return_value=FakeContext()):
        deleted = database_maintenance._delete_expired_batch(
            "makerhub_state_events",
            retention_days=14,
            batch_size=1000,
        )

    assert deleted == 37
    assert len(calls) == 1
    sql, params = calls[0]
    assert "created_at < now()" in sql
    assert "LIMIT %s" in sql
    assert "DELETE FROM makerhub_state_events" in sql
    assert params == (14, 1000)


def test_cleanup_is_bounded_and_zero_disables_each_table():
    calls = []
    responses = {
        "makerhub_state_events": [1000, 1000, 5],
        "makerhub_logs": [],
    }

    def fake_delete(table, *, retention_days, batch_size):
        calls.append((table, retention_days, batch_size))
        return responses[table].pop(0)

    with patch.object(database_maintenance, "_delete_expired_batch", side_effect=fake_delete):
        result = database_maintenance.cleanup_expired_rows(
            event_days=14,
            log_days=0,
            batch_size=1000,
            max_batches=10,
        )

    assert result["events_deleted"] == 2005
    assert result["logs_deleted"] == 0
    assert len(calls) == 3
    assert all(table == "makerhub_state_events" for table, _days, _size in calls)


def test_cleanup_limits_batches_and_keeps_table_failures_independent():
    calls = []

    def fake_delete(table, *, retention_days, batch_size):
        calls.append(table)
        if table == "makerhub_state_events":
            raise RuntimeError("event cleanup failed")
        return batch_size

    with patch.object(database_maintenance, "_delete_expired_batch", side_effect=fake_delete):
        result = database_maintenance.cleanup_expired_rows(
            event_days=14,
            log_days=90,
            batch_size=100,
            max_batches=2,
        )

    assert result["events_deleted"] == 0
    assert result["logs_deleted"] == 200
    assert "events" in result["errors"]
    assert calls == ["makerhub_state_events", "makerhub_logs", "makerhub_logs"]


def test_maintenance_scheduler_runs_once_per_interval():
    database_maintenance._reset_database_maintenance_for_tests()
    with patch.object(
        database_maintenance,
        "cleanup_expired_rows",
        return_value={"events_deleted": 1, "logs_deleted": 2, "errors": {}},
    ) as cleanup:
        first = database_maintenance.run_database_maintenance_if_due(now=1000, interval_seconds=300)
        skipped = database_maintenance.run_database_maintenance_if_due(now=1200, interval_seconds=300)
        second = database_maintenance.run_database_maintenance_if_due(now=1301, interval_seconds=300)

    assert first["ran"] is True
    assert skipped["ran"] is False
    assert second["ran"] is True
    assert cleanup.call_count == 2


def test_state_event_cursor_continues_across_retention_gaps():
    rows = [{"id": 101, "type": "state.changed", "scope": "archive_queue", "payload": {}}]

    class FakeResult:
        def fetchall(self):
            return rows

    class FakeConnection:
        def execute(self, sql, params=None):
            assert "id > %s" in sql
            assert params == (80, 100)
            return FakeResult()

    class FakeContext:
        def __enter__(self):
            return FakeConnection()

        def __exit__(self, exc_type, exc, tb):
            return False

    from app.core import database

    with patch.object(database, "initialize_database", return_value=True), \
            patch.object(database, "database_connection", return_value=FakeContext()):
        events = database.list_state_events_after(80)

    assert [item["id"] for item in events] == [101]


def test_worker_logs_one_retention_summary_when_rows_are_deleted():
    from app import worker

    result = {
        "ran": True,
        "events_deleted": 12,
        "logs_deleted": 34,
        "errors": {},
    }
    with patch.object(worker, "run_database_maintenance_if_due", return_value=result), \
            patch.object(worker, "append_business_log") as append_log:
        returned = worker._run_database_maintenance()

    assert returned == result
    append_log.assert_called_once()
    assert append_log.call_args.args[:2] == ("database", "retention_cleanup_completed")


def test_worker_does_not_log_skipped_retention_tick():
    from app import worker

    with patch.object(worker, "run_database_maintenance_if_due", return_value={"ran": False}), \
            patch.object(worker, "append_business_log") as append_log:
        worker._run_database_maintenance()

    append_log.assert_not_called()
