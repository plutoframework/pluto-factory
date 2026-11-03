"""Remote CI must pass at the validated head before requesting a merge."""

import pytest

from pluto_factory.github_checks import merge_check_status

HEAD = "a" * 40


def status(checks, required=None, **changes):
    pull_request = {
        "head": HEAD,
        "observed_head": HEAD,
        "head_changed": False,
        "checks": checks,
        **changes,
    }
    return merge_check_status(
        pull_request, ["validate"] if required is None else required
    )


@pytest.mark.parametrize(
    "checks,ready",
    [
        ([], False),
        ([{"name": "validate", "status": "QUEUED"}], False),
        ([{"name": "validate", "status": "IN_PROGRESS"}], False),
        (
            [
                {
                    "name": "validate",
                    "status": "COMPLETED",
                    "conclusion": "SUCCESS",
                }
            ],
            True,
        ),
        (
            [
                {
                    "name": "validate",
                    "status": "COMPLETED",
                    "conclusion": "FAILURE",
                }
            ],
            False,
        ),
        (
            [
                {
                    "name": "validate",
                    "status": "COMPLETED",
                    "conclusion": "SKIPPED",
                }
            ],
            False,
        ),
        (
            [
                {
                    "name": "validate",
                    "status": "COMPLETED",
                    "conclusion": "NEUTRAL",
                }
            ],
            False,
        ),
        (
            [
                {
                    "name": "validate",
                    "status": "COMPLETED",
                    "conclusion": "CANCELLED",
                }
            ],
            False,
        ),
        ([{"context": "validate", "state": "SUCCESS"}], True),
        ([{"context": "validate", "state": "PENDING"}], False),
        ([{"context": "validate", "state": "ERROR"}], False),
    ],
)
def test_named_check_requires_an_observed_success(checks, ready):
    assert status(checks)["ready"] is ready


def test_no_ci_is_compatible_only_without_named_requirements():
    assert status([], required=[])["ready"]
    assert not status([])["ready"]


@pytest.mark.parametrize(
    "check",
    [
        {"name": "optional", "status": "QUEUED"},
        {"name": "optional", "status": "COMPLETED", "conclusion": "FAILURE"},
        {"context": "optional", "state": "PENDING"},
        {"context": "optional", "state": "ERROR"},
    ],
)
def test_observed_pending_or_failing_checks_block_even_without_required_names(
    check,
):
    assert not status([check], required=[])["ready"]


def test_required_names_match_exactly():
    result = status([{"name": "Validate", "conclusion": "SUCCESS"}])
    assert not result["ready"]
    assert result["missing"] == ["validate"]


def test_check_success_from_another_head_cannot_satisfy_requirement():
    check = {"name": "validate", "conclusion": "SUCCESS", "head_sha": "b" * 40}
    assert not status([check])["ready"]
    assert not status([], required=[], observed_head="b" * 40)["ready"]


def test_old_success_does_not_hide_a_pending_rerun_or_another_failure():
    success = {"name": "validate", "conclusion": "SUCCESS"}
    pending = {
        "name": "validate",
        "status": "IN_PROGRESS",
        "conclusion": "SUCCESS",
    }
    assert not status([success, pending])["ready"]
    assert not status([success, {"name": "another", "conclusion": "FAILURE"}])[
        "ready"
    ]


def test_unknown_check_shapes_and_invalid_requirements_fail_closed():
    assert not status(["unrecognized"])["ready"]
    assert not status(
        [{"name": "validate", "status": "UNKNOWN", "conclusion": "SUCCESS"}]
    )["ready"]
    assert not status([], required=[""])["ready"]
