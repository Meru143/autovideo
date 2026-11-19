"""The video factory (design: docs/superpowers/specs/2026-11-03-video-factory-design.md). Run from the repo root:

    .venv/Scripts/python -m factory --help
"""
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = Path(os.environ.get("FACTORY_OUT", ROOT / "factory" / "out"))  # runtime files, git-ignored
RESULTS = ROOT / "factory" / "results" / "e2e"  # selftest artifacts (AGENTS.md)
