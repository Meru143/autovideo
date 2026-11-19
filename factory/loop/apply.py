"""The learning loop's weekly step (Plan 3b Task 5; rulings of 9 Nov: no Telegram, no operator input). It judges the
change in flight: kept, or rolled back by a revert when its 5 videos fell below the 25th percentile of the 10 before
it. With nothing in flight and enough evidence, it proposes one change, benchmarks it, and on a win applies it to the
live channel as one commit by "factory loop", never pushed. Someone's uncommitted edits make it wait; a revert that
conflicts is aborted and the change kept. It runs once a week per channel, in its own process
(FAILURE_MODES.md, loop)."""
from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .. import OUT, ROOT
from ..claude_call import SENSOR
from .benchmark import benchmark, items
from .evidence import evidence
from .propose import allowlist, propose

AUTHOR = "factory loop <loop@autovideo.invalid>"
WEEK = timedelta(days=7)


def record(root: Path, name: str) -> list[dict]:
    f = root / "loop" / f"{name}.jsonl"
    return [json.loads(x) for x in f.read_text(encoding="utf-8").splitlines() if x.strip()] if f.exists() else []


def _note(root: Path, name: str, entry: dict) -> None:
    (root / "loop").mkdir(parents=True, exist_ok=True)
    with (root / "loop" / f"{name}.jsonl").open("a", encoding="utf-8") as f:
        f.write(json.dumps(entry, default=str) + "\n")


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, timeout=120)


def _editable(ws: dict) -> list[Path]:
    books, _ = allowlist(ws)
    return [Path(ws["dir"]) / ws["playbooks"][b] for b in books if b in ws["playbooks"]] + [Path(ws["dir"]) / "settings.json"]


def _live(ws: dict, p: dict) -> Path:
    if p["target"] == "playbook":
        return Path(ws["dir"]) / ws["playbooks"][Path(p["path"]).stem]
    return Path(ws["dir"]) / "settings.json"


def _write(ws: dict, p: dict) -> None:
    path = _live(ws, p)
    if p["target"] == "playbook":
        path.write_text(p["change"], encoding="utf-8")
        return
    settings = json.loads(path.read_text(encoding="utf-8-sig"))
    *head, last = str(p["path"]).split(".")
    node = settings
    for k in head:
        node = node[k]
    node[last] = p["change"]
    path.write_text(json.dumps(settings, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def step(conn, ws: dict, now: datetime, *, root: Path = OUT, repo: Path = ROOT, propose_fn=propose,
         bench_fn=benchmark, claude_bin: list[str] | None = None, sensor: Path = SENSOR) -> dict:
    name, at = ws["name"], now.isoformat()
    ev = evidence(conn, name, now, root=root, record=record(root, name))
    if ev is None:
        return {"status": "waiting for evidence"}
    flight = ev["in_flight"]
    if flight and flight["verdict"] == "waiting":
        return {"status": "change in flight", **flight}
    if flight and flight["verdict"] == "keep":
        _note(root, name, {"at": at, "status": "kept", **{k: flight.get(k) for k in ("commit", "median_since", "floor")}})
    elif flight:  # revert: the 5 videos made under it fell below the floor of the 10 before it
        r = _git(repo, "revert", "--no-commit", flight["commit"])
        c = _git(repo, "commit", "--author", AUTHOR, "-m", f"loop({name}): roll back {flight['commit'][:7]}", "-m",
                 f"The 5 videos made under it reached a median day-7 retention of {flight['median_since']}%, under "
                 f"the 25th percentile ({flight['floor']}%) of the 10 made before it.") if r.returncode == 0 else r
        if c.returncode != 0:  # a later edit conflicts: abort, keep the change, and move on
            _git(repo, "revert", "--abort")
            _note(root, name, {"at": at, "status": "revert conflict", "commit": flight["commit"],
                               "error": (r.stderr or c.stderr)[-300:]})
            return {"status": "revert conflict", "commit": flight["commit"]}
        sha = _git(repo, "rev-parse", "HEAD").stdout.strip()
        _note(root, name, {"at": at, "status": "reverted", "commit": flight["commit"], "revert": sha,
                           "median_since": flight["median_since"], "floor": flight["floor"]})
        return {"status": "reverted", "commit": flight["commit"], "revert": sha}
    dirty = [str(p) for p in _editable(ws) if _git(repo, "status", "--porcelain", "--", str(p)).stdout.strip()]
    if dirty:  # someone is editing these files: wait, spending nothing
        return {"status": "waiting: uncommitted edits", "files": dirty}
    p = propose_fn(ws, ev, claude_bin=claude_bin, sensor=sensor)
    if "refused" in p:
        _note(root, name, {"at": at, "status": "refused", "why": p["refused"]})
        return {"status": "refused", "why": p["refused"]}
    b = bench_fn(ws, p, items(root, [*ev["best"], *reversed(ev["worst"])]), claude_bin=claude_bin, sensor=sensor,
                 root=root)
    if b.get("verdict") != "win":
        _note(root, name, {"at": at, "status": "lost", "proposal": p, "benchmark": b})
        return {"status": "lost", "benchmark": b}
    path = _live(ws, p)
    before = path.read_bytes()
    _write(ws, p)
    summary = f"loop({name}): {str(p.get('rationale') or 'one change').splitlines()[0][:72]}"
    body = json.dumps({"change": {k: p.get(k) for k in ("target", "path", "expected")},
                       "benchmark": {k: b.get(k) for k in ("wins", "losses", "seed")},
                       "evidence": {"median": ev["median"], "top": ev["top"], "videos": len(ev["videos"])}}, indent=1)
    c = _git(repo, "commit", "--author", AUTHOR, "-m", summary, "-m", body, "--", str(path))
    if c.returncode != 0:
        path.write_bytes(before)
        _note(root, name, {"at": at, "status": "commit failed", "error": c.stderr[-300:]})
        return {"status": "commit failed", "error": c.stderr[-300:]}
    sha = _git(repo, "rev-parse", "HEAD").stdout.strip()
    _note(root, name, {"at": at, "status": "applied", "commit": sha, "path": p["path"], "summary": summary,
                       "proposal": p, "benchmark": b})
    return {"status": "applied", "commit": sha}


def _spawn(name: str, root: Path) -> None:
    """The step in its own process, detached, so the tick never waits on a benchmark."""
    (root / "loop").mkdir(parents=True, exist_ok=True)
    base = (subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP) if os.name == "nt" else 0
    with (root / "loop" / f"{name}.log").open("ab") as out:
        for flags in ((base | 0x01000000, base) if os.name == "nt" else (base,)):  # leave Task Scheduler's job
            try:
                subprocess.Popen([sys.executable, "-m", "factory", "loop", name], cwd=ROOT, stdin=subprocess.DEVNULL,
                                 stdout=out, stderr=subprocess.STDOUT, creationflags=flags)
                return
            except OSError:
                continue


def weekly(now: datetime | None = None, *, root: Path = OUT, spawn=_spawn) -> list[str]:
    """After the tick: each active channel's step, once a week, marked before it starts. Returns those started."""
    from ..scheduler import _scheduled
    now = now or datetime.now(timezone.utc)
    mark = root / "loop" / "state.json"
    state = json.loads(mark.read_text(encoding="utf-8")) if mark.exists() else {}
    started = []
    for name, w in _scheduled():
        if w["schedule"].get("paused") or (state.get(name) and now - datetime.fromisoformat(state[name]) < WEEK):
            continue
        state[name] = now.isoformat()
        mark.parent.mkdir(parents=True, exist_ok=True)
        mark.write_text(json.dumps(state), encoding="utf-8")
        spawn(name, root)
        started.append(name)
    return started
