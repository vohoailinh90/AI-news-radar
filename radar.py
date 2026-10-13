# layout: entry-point
"""Launcher: `python radar.py scan | export | run | verify` (see README.md)."""

import sys

from radar.cli import main

if __name__ == "__main__":
    sys.exit(main())
