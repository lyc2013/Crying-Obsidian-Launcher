"""Allow running the CLI with `python -m mc_launcher`."""

import sys

from .cli import main

if __name__ == "__main__":
    sys.exit(main())
