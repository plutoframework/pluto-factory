"""Pure, bounded recovery policy. Notification and blocking are independent."""

from dataclasses import dataclass

from .models import Failure


@dataclass(frozen=True)
class Recovery:
    action: str
    delay: float
    notify: str


def plan_recovery(
    failure: Failure,
    recoveries: int,
    maximum: int,
    base_delay: float,
) -> Recovery:
    if failure.code == "storage.unavailable":
        return Recovery("pause_factory", 0, "urgent")
    if failure.uncertain:
        return Recovery("reconcile", 0, "progress")
    if failure.code in {
        "permission.required",
        "auth.required",
        "quota.exhausted",
        "config.invalid",
        "validation.unconfigured",
        "scope.denied",
        "github.no_changes",
    }:
        return Recovery("defer", 0, "action_required")
    if recoveries >= maximum:
        return Recovery("defer", 0, "action_required")
    delay = failure.retry_after_seconds
    if delay is None:
        delay = min(base_delay * (2**recoveries), 300)
    return Recovery("retry", delay, "progress")
