from pluto_factory.models import FactoryConfig, Repository
from pluto_factory.notifications import flush
from pluto_factory.state import State


def test_notification_failure_remains_pending_without_blocking_work(
    tmp_path, monkeypatch
):
    state = State(tmp_path)
    config = FactoryConfig(
        name="test",
        repositories=[Repository(name="core", path=str(tmp_path))],
        slack_webhook_env="FACTORY_TEST_WEBHOOK",
    )
    monkeypatch.setenv("FACTORY_TEST_WEBHOOK", "https://example.invalid/test")
    state.notify(None, "progress", "Recovering", "incident")

    def reject(_url, _payload):
        raise OSError("unavailable")

    assert flush(state, config, sender=reject)["delivered"] == 0
    assert not state.is_paused()
    pending = state.inspect()["notifications"][0]
    assert pending["state"] == "pending"
    assert pending["attempts"] == 1
    with state.db:
        state.db.execute("UPDATE notifications SET wake_at=0")
    delivered = []
    assert (
        flush(
            state, config, sender=lambda url, payload: delivered.append(payload)
        )["delivered"]
        == 1
    )
    assert delivered[0]["text"].endswith("Recovering")


def test_no_destination_never_sends_and_incident_is_deduplicated(tmp_path):
    state = State(tmp_path)
    config = FactoryConfig(
        name="test", repositories=[Repository(name="core", path=str(tmp_path))]
    )
    state.notify(None, "progress", "Recovering", "incident")
    state.notify(None, "progress", "Recovering again", "incident")
    assert flush(state, config) == {"delivered": 0, "configured": False}
    assert len(state.inspect()["notifications"]) == 1
