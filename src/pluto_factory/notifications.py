"""An independent, retryable Slack outbox; no configured channel is assumed."""

import json
import os
import time
from urllib.error import URLError
from urllib.request import Request, urlopen


def flush(state, config, sender=None):
    variable = config.slack_webhook_env
    url = os.environ.get(variable, "") if variable else ""
    if not url:
        return {"delivered": 0, "configured": False}
    if (
        not url.startswith("https://hooks.slack.com/services/")
        and sender is None
    ):
        raise ValueError("Slack webhook must use the official HTTPS endpoint")
    delivered = 0
    rows = state.db.execute(
        "SELECT * FROM notifications WHERE state='pending' AND wake_at<=?",
        (time.time(),),
    ).fetchall()
    for row in rows:
        text = f"[{config.name}] {row['kind']}: {row['message']}"
        try:
            if sender:
                sender(url, {"text": text})
            else:
                request = Request(
                    url,
                    data=json.dumps({"text": text}).encode(),
                    headers={"Content-Type": "application/json"},
                    method="POST",
                )
                with urlopen(request, timeout=10) as response:
                    if response.status != 200:
                        raise RuntimeError("notification delivery rejected")
        except (OSError, URLError, RuntimeError):
            with state.db:
                state.db.execute(
                    "UPDATE notifications SET attempts=attempts+1,"
                    "last_error='delivery failed',wake_at=? WHERE id=?",
                    (
                        time.time() + min(5 * 2 ** row["attempts"], 300),
                        row["id"],
                    ),
                )
        else:
            with state.db:
                state.db.execute(
                    "UPDATE notifications SET state='delivered',"
                    "attempts=attempts+1,last_error=NULL WHERE id=?",
                    (row["id"],),
                )
            delivered += 1
    return {"delivered": delivered, "configured": True}
