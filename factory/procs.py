"""One way to run a long child process (final review Important 2; ported from spikes/bakeoff/bakeoff/procs.py): a
timeout kills the whole tree (Chrome workers, ffmpeg, a grandchild holding the pipes), then waits at most 5 s for the
pipes; Windows' transient spawn EBUSY is retried (FAILURE_MODES.md, procs)."""
from __future__ import annotations

import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

import psutil


@dataclass
class RunResult:
    returncode: int  # -1 after a timeout
    stdout: str
    stderr: str
    seconds: float
    timed_out: bool


def kill_tree(pid: int) -> None:
    """Children first, then the parent. ponytail: walks from a live parent; a child that already exited leaves its
    own children unfound (psutil can't walk from a dead PID). Scan process_iter by ppid if that ever bites."""
    try:
        parent = psutil.Process(pid)
        procs = [*parent.children(recursive=True), parent]
    except psutil.NoSuchProcess:
        return
    for proc in procs:
        try:
            proc.kill()
        except psutil.NoSuchProcess:
            pass


def _watch(p: subprocess.Popen, input: bytes | None, timeout_s: float | None, stall_s: float) -> tuple[bytes, bytes, bool]:
    """Read the child's output as it comes and kill its tree only when it has said nothing for stall_s (or passed
    timeout_s, if one is given): a long run that keeps working is never cut (operator, 6 Nov: no 60 min limit)."""
    import threading
    chunks: dict[str, list[bytes]] = {"out": [], "err": []}
    last = [time.monotonic()]

    def pump(stream, key: str) -> None:
        for line in iter(stream.readline, b""):
            chunks[key].append(line)
            last[0] = time.monotonic()
    readers = [threading.Thread(target=pump, args=(p.stdout, "out"), daemon=True),
               threading.Thread(target=pump, args=(p.stderr, "err"), daemon=True)]
    for r in readers:
        r.start()
    if input is not None:
        try:
            p.stdin.write(input)
            p.stdin.close()
        except OSError:
            pass
    start, timed_out = time.monotonic(), False
    while p.poll() is None:
        time.sleep(0.2)
        now = time.monotonic()
        if now - last[0] > stall_s or (timeout_s is not None and now - start > timeout_s):
            timed_out = True
            kill_tree(p.pid)
            break
    for r in readers:
        r.join(timeout=5)  # a survivor holding the pipes: its tail is lost, never waited on
    return b"".join(chunks["out"]), b"".join(chunks["err"]), timed_out


def run_cli(cmd: list[str], *, cwd: Path | None = None, env: Mapping[str, str] | None = None,
            timeout_s: float | None, stall_s: float | None = None, input: bytes | None = None,
            ebusy_retries: int = 0) -> RunResult:
    """timeout_s: a wall clock for the whole run. stall_s: kill only after that long with no output at all, for long
    writer runs that keep talking (Claude's stream-json prints an event per step)."""
    t0, attempt = time.perf_counter(), 0
    while True:
        p = subprocess.Popen(cmd, cwd=cwd, env=None if env is None else dict(env),
                             stdin=subprocess.DEVNULL if input is None else subprocess.PIPE,
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        timed_out = False
        if stall_s is not None:
            out, err, timed_out = _watch(p, input, timeout_s, stall_s)
        else:
            try:
                out, err = p.communicate(input, timeout=timeout_s)
            except subprocess.TimeoutExpired:
                timed_out = True
                kill_tree(p.pid)
                try:
                    out, err = p.communicate(timeout=5)
                except subprocess.TimeoutExpired:  # a survivor still holds the pipes: its output is lost
                    out, err = b"", b""
        stdout, stderr = out.decode("utf-8", "replace"), err.decode("utf-8", "replace")
        if not timed_out and p.returncode != 0 and "EBUSY" in stderr and attempt < ebusy_retries:
            attempt += 1
            time.sleep(2 ** attempt)
            continue
        return RunResult(-1 if timed_out else p.returncode, stdout, stderr, round(time.perf_counter() - t0, 1),
                         timed_out)
