"""Exercise the Snake project's locked QA setup without host overrides."""

import json
import os
import subprocess
import sys

OVERRIDES = (
    "PLUTO_PLAYWRIGHT_MODULE",
    "PLUTO_BROWSER_CHANNEL",
    "PLUTO_QA_ARTIFACT_DIR",
)


def run_portable_qa(runner=subprocess.run):
    environment = os.environ.copy()
    for key in OVERRIDES:
        environment.pop(key, None)
    print(
        json.dumps(
            {
                "validation": "portable-project-qa",
                "factory_overrides_removed": list(OVERRIDES),
                "feature_profile": environment.get("SNAKE_FEATURES", "none"),
            }
        ),
        flush=True,
    )
    for arguments in (
        ["npm", "ci"],
        ["npx", "--no-install", "playwright", "install", "chromium"],
        ["npm", "run", "qa"],
    ):
        result = runner(arguments, env=environment, check=False)
        if result.returncode:
            return result.returncode
    return 0


if __name__ == "__main__":
    sys.exit(run_portable_qa())
