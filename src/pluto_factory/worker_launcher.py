"""Pass stored credential environment to a worker without secret argv."""

import os
import sys
from pathlib import Path

from .credentials import credential_environment


def main():
    if len(sys.argv) < 3:
        raise SystemExit("worker launcher needs factory home and executable")
    home = Path(sys.argv[1]).resolve(strict=True)
    environment = os.environ.copy()
    environment.update(credential_environment(home))
    os.execvpe(sys.argv[2], sys.argv[2:], environment)


if __name__ == "__main__":
    main()
