from app.services import state_contracts


def test_core_state_keys_are_stable():
    assert state_contracts.ARCHIVE_QUEUE_STATE_KEY == "archive_queue"
    assert state_contracts.MISSING_3MF_STATE_KEY == "missing_3mf"
    assert state_contracts.ORGANIZE_TASKS_STATE_KEY == "organize_tasks"
    assert state_contracts.SUBSCRIPTIONS_STATE_KEY == "subscriptions_state"
    assert state_contracts.REMOTE_REFRESH_STATE_KEY == "remote_refresh_state"


def test_state_event_scopes_cover_dashboard_consumers():
    scopes = state_contracts.dashboard_event_scopes()
    assert scopes == [
        "archive_queue",
        "missing_3mf",
        "organize_tasks",
        "subscriptions_state",
        "remote_refresh_state",
    ]


def test_status_sets_include_existing_values():
    assert {"queued", "running", "completed", "failed"}.issubset(state_contracts.ARCHIVE_TASK_STATUSES)
    assert {"missing", "queued", "failed", "download_limited"}.issubset(state_contracts.MISSING_3MF_STATUSES)
    assert {"idle", "running", "success", "error", "disabled"}.issubset(state_contracts.REMOTE_REFRESH_STATUSES)
    assert {"idle", "running", "success", "error", "pending"}.issubset(state_contracts.SUBSCRIPTION_STATUSES)
