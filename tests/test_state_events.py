from app.services import state_events


def test_state_event_callbacks_only_wake_matching_scopes():
    received = []
    unregister_archive = state_events.register_state_event_callback(
        lambda: received.append("archive"),
        scopes=["archive_queue"],
    )
    unregister_models = state_events.register_state_event_callback(
        lambda: received.append("models"),
        scopes=["models"],
    )
    try:
        state_events.wake_state_event_subscribers("archive_queue")
    finally:
        unregister_archive()
        unregister_models()

    assert received == ["archive"]
