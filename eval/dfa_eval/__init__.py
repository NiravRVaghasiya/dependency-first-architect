"""Dependency-First Architect evaluation harness: generate, blind, judge, lint, aggregate, report.

Standard library only. Entry point: `python eval/run.py --help`. Every stage reads and writes a
self-contained run directory (see `records.py`), so committed runs can be re-checked offline.
HARNESS_VERSION is recorded in every run manifest; bump it when a change to the harness could
change what a run records.
"""

HARNESS_VERSION = "1.0.0"
