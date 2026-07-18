from app import worker


def test_idle_worker_uses_backoff_after_archive_and_index_work_are_quiet():
    resolver = getattr(worker, "worker_poll_seconds", None)
    assert callable(resolver), "worker must expose worker_poll_seconds()"

    assert resolver({"queued_count": 0, "running_count": 0}, rebuild_running=False) == worker.WORKER_IDLE_POLL_SECONDS
    assert resolver({"queued_count": 1, "running_count": 0}, rebuild_running=False) == worker.WORKER_POLL_SECONDS
    assert resolver({"queued_count": 0, "running_count": 1}, rebuild_running=False) == worker.WORKER_POLL_SECONDS
    assert resolver({"queued_count": 0, "running_count": 0}, rebuild_running=True) == worker.WORKER_POLL_SECONDS


def test_worker_heartbeat_interval_stays_within_readiness_window():
    assert worker.WORKER_HEARTBEAT_INTERVAL_SECONDS < worker.WORKER_HEARTBEAT_MAX_AGE_SECONDS
