import json
import time
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from pluto_factory.coordinator import Coordinator, CoordinatorError
from pluto_factory.models import Failure
from pluto_factory.ratelimit import (
    FALLBACK_SECONDS,
    MARGIN_SECONDS,
    limit_failure,
    seconds_until_reset,
    transcript_failure,
)
from pluto_factory.recovery import plan_recovery

LA = ZoneInfo("America/Los_Angeles")
NIGHT = datetime(2026, 11, 15, 2, 55, tzinfo=LA).timestamp()
MESSAGE = "You've hit your 5-hour limit · resets 4am (America/Los_Angeles)"


def test_reset_time_parsed_in_named_zone():
    wait = seconds_until_reset(MESSAGE, NIGHT)
    assert wait == pytest.approx(65 * 60 + MARGIN_SECONDS)


def test_reset_time_already_passed_rolls_to_tomorrow():
    late = datetime(2026, 11, 15, 5, 0, tzinfo=LA).timestamp()
    assert seconds_until_reset(MESSAGE, late) == pytest.approx(
        23 * 3600 + MARGIN_SECONDS
    )


def test_reset_with_minutes_pm_and_date():
    text = "Weekly limit reached ∙ resets Nov 20, 4:30pm (America/Los_Angeles)"
    expected = datetime(2026, 11, 20, 16, 30, tzinfo=LA).timestamp() - NIGHT
    assert seconds_until_reset(text, NIGHT) == pytest.approx(
        expected + MARGIN_SECONDS
    )


def test_legacy_epoch_message():
    reset = int(NIGHT) + 600
    text = f"Claude AI usage limit reached|{reset}"
    assert seconds_until_reset(text, NIGHT) == pytest.approx(
        reset - NIGHT + MARGIN_SECONDS
    )


@pytest.mark.parametrize(
    "text",
    [
        MESSAGE,
        "5-hour limit reached ∙ resets 4am",
        "Claude usage limit reached. Your limit will reset at 4am (America/Los_Angeles).",
        'API Error: 429 {"type":"error","error":{"type":"rate_limit_error"}}',
        "API Error: 529 overloaded_error",
    ],
)
def test_limits_are_certain_rate_limits(text):
    failure = limit_failure(text, now=NIGHT)
    assert failure.code == "provider.rate_limited"
    assert failure.uncertain is False
    assert failure.retry_after_seconds > 0


def test_unstated_reset_uses_fallback_and_plain_text_is_ignored():
    assert (
        limit_failure("API Error: 529 overloaded_error").retry_after_seconds
        == FALLBACK_SECONDS
    )
    assert limit_failure("All tests passed") is None


def _transcript(tmp_path, *entries):
    path = tmp_path / "session.jsonl"
    path.write_text("\n".join(json.dumps(e) for e in entries) + "\n")
    return path


def _api_error(text):
    return {
        "type": "assistant",
        "isApiErrorMessage": True,
        "message": {"content": [{"type": "text", "text": text}]},
    }


def test_transcript_limit_detected(tmp_path):
    path = _transcript(
        tmp_path,
        {"type": "user", "message": {"content": "work"}},
        _api_error(MESSAGE),
    )
    failure = transcript_failure(path, NIGHT)
    assert failure.code == "provider.rate_limited"
    assert failure.retry_after_seconds == pytest.approx(
        65 * 60 + MARGIN_SECONDS
    )


def test_transcript_prose_about_limits_is_not_a_limit(tmp_path):
    path = _transcript(
        tmp_path,
        {
            "type": "assistant",
            "message": {"content": [{"type": "text", "text": MESSAGE}]},
        },
    )
    assert transcript_failure(path) is None
    assert transcript_failure(tmp_path / "missing.jsonl") is None
    assert transcript_failure(None) is None


def test_rate_limit_does_not_spend_recoveries_or_defer():
    failure = limit_failure(MESSAGE, now=NIGHT)
    for recoveries in (0, 3, 50):
        plan = plan_recovery(failure, recoveries, 3, 5)
        assert plan.action == "rate_limit"
        assert plan.delay == failure.retry_after_seconds


def test_coordinator_subscription_limit_is_rate_limited(tmp_path, monkeypatch):
    from test_engine import _claude_coordinator, _result

    coordinator, _ = _claude_coordinator(
        tmp_path,
        monkeypatch,
        _result(is_error=True, subtype="success", result=MESSAGE),
    )
    with pytest.raises(CoordinatorError) as error:
        coordinator.decide({}, {"advance"}, "Role")
    failure = error.value.failure
    assert failure.code == "provider.rate_limited"
    assert failure.uncertain is False
    assert failure.retry_after_seconds > 0
    assert isinstance(failure, Failure) and isinstance(coordinator, Coordinator)


def test_engine_waits_out_limit_without_deferring_or_spending_budget(tmp_path):
    from test_engine import ObservedHarness

    from pluto_factory.demo import prepare_demo
    from pluto_factory.engine import Engine

    home, prepared = prepare_demo(tmp_path / "limited")
    harness = ObservedHarness()
    engine = Engine(home, harness=harness)
    engine.config.max_recoveries = 0
    work_id = prepared["submitted"]["id"]
    run = engine.state.create_run(work_id, "implementer", {"id": "run-limit"})
    engine.state.update_run(run["id"], state="running", session={"owned": True})
    try:
        for _ in range(2):  # repeated limits stay a single notice
            engine._failure(work_id, limit_failure(MESSAGE, now=time.time()))
        work = engine.state.get_work(work_id)
        until = engine.state.rate_limited_until()
        assert work["control"] == "backoff"
        assert work["recoveries"] == 0
        assert work["wake_at"] == until > time.time()
        assert engine.state.get_run(run["id"])["state"] == "failed"
        notices = engine.state.db.execute(
            "SELECT * FROM notifications WHERE incident_key LIKE 'ratelimit:%'"
        ).fetchall()
        assert len(notices) == 1
        # Nothing dispatches while limited, even other ready work.
        assert engine._limited()
        assert engine.tick() is False
        # After the reset the limit clears and dispatch resumes.
        engine.state.set_rate_limit(0)  # never shortens
        assert engine.state.rate_limited_until() == until
        with engine.state.db:
            engine.state.db.execute(
                "UPDATE settings SET value=? WHERE key='rate_limited_until'",
                (str(time.time() - 1),),
            )
        engine.state.update_work(work_id, wake_at=0)
        assert not engine._limited()
        assert engine.state.rate_limited_until() == 0
        assert engine.tick() is True
    finally:
        engine.state.close()
