"""Pass stored credential environment to a worker without secret argv."""

import os
import sys
import tempfile
from pathlib import Path

from .credentials import credential_environment

# Workers never publish: the factory alone pushes and opens PRs. Any GitHub
# credential in the inherited environment is removed and git/gh are pointed
# at empty configuration so `git push` and `gh pr create` cannot authenticate.
GITHUB_CREDENTIALS = (
    "GH_TOKEN",
    "GITHUB_TOKEN",
    "GH_ENTERPRISE_TOKEN",
    "GITHUB_ENTERPRISE_TOKEN",
    "SSH_AUTH_SOCK",
    "GIT_ASKPASS",
    "SSH_ASKPASS",
)


def worker_environment(base, empty_gh_config):
    environment = {
        key: value
        for key, value in base.items()
        if key not in GITHUB_CREDENTIALS
    }
    environment.update(
        {
            "GH_CONFIG_DIR": empty_gh_config,
            "GH_PROMPT_DISABLED": "1",
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_CONFIG_SYSTEM": os.devnull,
            "GIT_ASKPASS": "/bin/false",
            "GIT_SSH_COMMAND": "/bin/false",
        }
    )
    return environment


def main():
    if len(sys.argv) < 3:
        raise SystemExit("worker launcher needs factory home and executable")
    home = Path(sys.argv[1]).resolve(strict=True)
    environment = os.environ.copy()
    environment.update(credential_environment(home))
    environment = worker_environment(
        environment, tempfile.mkdtemp(prefix="pf-no-gh-")
    )
    os.execvpe(sys.argv[2], sys.argv[2:], environment)


if __name__ == "__main__":
    main()
