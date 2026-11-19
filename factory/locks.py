"""A lock the operating system holds for a process and drops when the process ends, however it ends (a crash, a power cut,
a kill). A lock file left behind blocks nothing, and a reused process number means nothing (9 Nov: the PID files could
freeze every channel after a restart). The file stays; only its first byte is locked, and never deleting it keeps two
takers from locking two different files."""
from __future__ import annotations

import os
import time
from contextlib import contextmanager
from pathlib import Path


def _take(fd: int) -> bool:
    os.lseek(fd, 0, os.SEEK_SET)
    try:
        if os.name == "nt":
            import msvcrt
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return True
    except OSError:
        return False


@contextmanager
def held(path: Path, *, wait: bool, poll_s: float = 2.0):
    """Yields True while this process holds the lock on path. With wait, it waits for the holder to let go; without,
    it yields False at once when another holder has it."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_RDWR | os.O_CREAT)
    try:
        got = _take(fd)
        while wait and not got:
            time.sleep(poll_s)
            got = _take(fd)
        try:
            yield got
        finally:
            if got and os.name == "nt":  # released on close too, but Windows may take its time
                import msvcrt
                os.lseek(fd, 0, os.SEEK_SET)
                msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
    finally:
        os.close(fd)
