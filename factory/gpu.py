"""One heavy GPU job at a time (operator rule), now that videos run side by side (9 Nov): the film render, the music
render, the cloned voice and the local transcription each hold this lock while they run. The OS drops it when its
holder ends, however it ends (factory/locks.py), and nested use in one process doesn't wait on itself."""
from __future__ import annotations

import contextlib
from pathlib import Path

from . import OUT
from .locks import held

_depth = 0


@contextlib.contextmanager
def gpu_lock(root: Path = OUT, poll_s: float = 2.0):
    global _depth
    with contextlib.nullcontext() if _depth else held(root / "gpu_os.lock", wait=True, poll_s=poll_s):
        _depth += 1
        try:
            yield
        finally:
            _depth -= 1
