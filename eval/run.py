#!/usr/bin/env python3
"""Evaluation harness entry point:  python eval/run.py --help

Generates plans in isolated sessions, blinds them, has independent models judge them, and
aggregates the raw records into summary.json and REPORT.md. Every stage reads and writes one
self-contained run directory. Standard library only; Python 3.9 or newer.
"""

import sys
from pathlib import Path

if sys.version_info < (3, 9):
    raise SystemExit("the evaluation harness needs Python 3.9 or newer")

sys.path.insert(0, str(Path(__file__).resolve().parent))

from dfa_eval import cli  # noqa: E402  (needs the path above)

if __name__ == "__main__":
    raise SystemExit(cli.main())
