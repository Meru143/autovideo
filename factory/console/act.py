"""The console's actions (console Task 3): every change the operator makes, each one calling factory code. Post now,
retry and drop are the scheduler's and jobs.py's own; settings and playbook edits are git commits (the repo's own
identity, `console(<channel>): <what>`), made only after the loader accepts them; an undo is `git revert`, taken back
by another when the loader refuses the settings it leaves. A refusal raises Conflict (409), Invalid (422) or NotFound
(404) before anything is written (an undo's 422, once its revert is taken back), and a failed edit or revert leaves
the file and the tree as they were. Run in the repo's folder as git sees it: `repo` is the repo holding the channels."""
from __future__ import annotations

import json
import os
import tempfile
import threading
from datetime import datetime, timezone
from pathlib import Path

from .. import jobs, scheduler, settings as factory_settings
from ..loop.apply import _note
from ..topics import store
from ..workspace import channels_root, load
from . import read

SCHEDULE = ("paused", "slots", "timezone", "max_per_day")
TOPICS = ("levers", "min_score", "ideas_per_pass")
_edits = threading.Lock()  # ponytail: one git edit at a time, in this process; git's own index.lock covers the rest


class Conflict(Exception):
    """409: busy, already done, or the repo moved under the edit."""


class Invalid(Exception):
    """422: the change would not load."""


class NotFound(Exception):
    """404: no such id or name."""


def _job(root: Path, jid: str) -> jobs.Job:
    try:
        return jobs.load(jid, root=root)
    except KeyError:
        raise NotFound(f"no video {jid}") from None


def post(root: Path, jid: str, *, anyway: bool, legs: dict | None) -> dict:
    """Post now / Post anyway: the tick's own publish routine (scheduler.post_now) under the tick lock."""
    _job(root, jid)
    try:
        out = scheduler.post_now(jid, anyway=anyway, root=root, legs=legs)
    except (scheduler.Busy, scheduler.AlreadyPosted, scheduler.NotReady) as e:
        raise Conflict(str(e)) from None
    if out["capped"]:
        raise Conflict("Already posted today")
    return {"ok": out["published"], "publish": out["publish"]}


def retry(root: Path, jid: str) -> str:
    """Reopen a stopped video with its regeneration budget restored, as the tick's own retry does."""
    job = _job(root, jid)
    with jobs._db(root) as c:
        state = c.execute("select state from jobs where id = ?", (jid,)).fetchone()[0]
    if state not in ("failed", "quarantined"):
        raise Conflict(f"{jid} isn't stopped (it is {state})")
    return jobs.reset(job.id, budget=True, root=root, by="console")


def drop(root: Path, jid: str) -> str:
    """Drop first, then end a running process: a stage that finishes in between can't move a dropped job on. A posted
    video is refused, on YouTube or by a recorded Reel alone (final review, 10 Nov), and so is one being posted
    (jobs.drop's rule). A busy tick lock leaves the kill to the tick, which ends dropped runs itself."""
    job = _job(root, jid)
    if any((job.dir / f).exists() for f in ("upload.json", "instagram.json")):
        raise Conflict("It's already posted")
    try:
        state = jobs.drop(jid, root=root)
    except jobs.Posting:
        raise Conflict("It's being posted right now") from None
    try:
        scheduler.stop_run(jid, root=root)
    except scheduler.Busy:
        pass
    return state


def _head(repo: Path) -> str:
    return read._git(repo, "rev-parse", "HEAD").stdout.strip()


def _write(path: Path, data: bytes) -> None:
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=path.name + ".")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def _put_back(path: Path, old: bytes) -> None:
    """Restore a file after a refused commit. Windows can refuse the replace while another process has the file open:
    retry once, then say so, naming the file (it holds the refused text, and running jobs read it)."""
    for attempt in (1, 2):
        try:
            return _write(path, old)
        except OSError as e:
            err = e
    raise Conflict(f"{path} couldn't be put back after git refused the commit ({err}): it holds the refused change, "
                   "so restore it with git checkout") from None


def _commit(repo: Path, path: Path, data: bytes, message: str, built_from: bytes | None = None) -> str:
    """Write the file and commit it alone; refuse while it has changes nobody committed, or when it is no longer the
    version `data` was built from (someone committed in between); undo the write if git says no."""
    old = path.read_bytes()
    if built_from is not None and old != built_from:
        raise Conflict(f"{path.name} changed while you were saving: reload and try again")
    if read._git(repo, "status", "--porcelain", "--", str(path)).stdout.strip():
        raise Conflict(f"{path.name} has changes that aren't committed")
    if data == old:
        raise Invalid("nothing changed")
    _write(path, data)
    r = read._git(repo, "commit", "-q", "-m", message, "--", str(path))
    if r.returncode:
        _put_back(path, old)
        raise Conflict(f"git refused the commit: {(r.stderr or r.stdout).strip()}")
    return _head(repo)


def _channel(name: str) -> dict:
    ws = read._channels().get(name)
    if ws is None:
        raise NotFound(f"no channel {name}")
    return ws


def save_settings(name: str, changes: dict, *, repo: Path) -> str:
    """A partial settings change, checked by workspace.load on a copy before it is written. Returns the commit."""
    path = Path(_channel(name)["dir"]) / "settings.json"
    with _edits:  # the read, the check and the commit are one step: nothing lands in between unseen
        return _save_settings(name, path, changes, repo)


def _save_settings(name: str, path: Path, changes: dict, repo: Path) -> str:
    raw = path.read_bytes()
    w = json.loads(raw.decode("utf-8-sig"))
    if not changes:
        raise Invalid("no change given")
    for k, v in changes.items():
        if k in SCHEDULE:
            if k == "paused" and type(v) is not bool:
                raise Invalid("paused must be true or false")
            if v is None and k == "max_per_day":  # no cap
                w["schedule"].pop(k, None)
            else:
                w["schedule"][k] = v
        elif k == "instagram":
            if type(v) is not bool:
                raise Invalid("instagram must be true or false")
            w.setdefault("instagram", {})["enabled"] = v
        elif k == "topics" and isinstance(v, dict) and set(v) <= set(TOPICS):
            w["topics"].update(v)
        else:
            raise Invalid(f"can't change {k!r}: the settings take {', '.join(SCHEDULE)}, instagram and "
                          f"topics ({', '.join(TOPICS)})")
    out = (json.dumps(w, indent=2, ensure_ascii=False) + ("\n" if raw.endswith(b"\n") else "")).encode("utf-8")
    with tempfile.TemporaryDirectory() as tmp:  # the loader reads <root>/<name>/settings.json
        (Path(tmp) / name).mkdir()
        (Path(tmp) / name / "settings.json").write_bytes(out)
        try:
            load(name, need=(), root=Path(tmp))  # the edit touches only what it checks even without the rest
        except ValueError as e:
            raise Invalid(str(e).partition("settings.json: ")[2] or str(e)) from None
    return _commit(repo, path, out, f"console({name}): {', '.join(changes)}", built_from=raw)


def save_factory_settings(changes: dict, *, repo: Path) -> str:
    """A partial change to factory/settings.json: top-level keys, with stop_at and steps merged one level deeper,
    checked by settings.check on the merged copy before it is written. Returns the commit."""
    path = factory_settings.path()
    if not changes:
        raise Invalid("no change given")
    with _edits:
        raw = path.read_bytes()
        try:
            w = json.loads(raw.decode("utf-8-sig"))
        except ValueError as e:
            raise Invalid(f"{path.name} doesn't load: {e}") from None
        for k, v in changes.items():
            if k not in factory_settings.KEYS:
                raise Invalid(f"can't change {k!r}: the settings take {', '.join(factory_settings.KEYS)}")
            if k == "steps" and isinstance(v, dict) and all(isinstance(x, dict) for x in v.values()):
                for step, c in v.items():
                    w["steps"][step] = {**w["steps"].get(step, {}), **c}
            elif k in ("stop_at", "fallback") and isinstance(v, dict):
                w[k] = {**w[k], **v}
            else:
                w[k] = v
        try:
            factory_settings.check(w)
        except ValueError as e:
            raise Invalid(str(e)) from None
        out = (json.dumps(w, indent=2, ensure_ascii=False) + ("\n" if raw.endswith(b"\n") else "")).encode("utf-8")
        return _commit(repo, path, out, f"console(factory): {', '.join(changes)}", built_from=raw)


def save_playbook(name: str, key: str, text: str, base: str, *, repo: Path) -> str:
    """Save a playbook, unless it changed since the editor loaded it (its last commit isn't `base`)."""
    ws = _channel(name)
    rel = ws.get("playbooks", {}).get(key)
    if rel is None:
        raise NotFound(f"no playbook {key} on {name}")
    path = Path(ws["dir"]) / rel
    if not isinstance(text, str) or not text.strip():
        raise Invalid("a playbook can't be empty")
    with _edits:
        last = read._git(repo, "log", "-1", "--format=%H", "--", str(path)).stdout.strip()
        if last != base:
            raise Conflict("the playbook changed since you opened it: reload it and edit again")
        data = text.replace("\r\n", "\n").encode("utf-8")
        if b"\r\n" in path.read_bytes():  # keep the file's own line ends: no whole-file diff
            data = data.replace(b"\n", b"\r\n")
        return _commit(repo, path, data, f"console({name}): edit {key} playbook")


def _abort_revert(repo: Path, sha: str) -> None:
    """Leave this undo's revert not half done: while REVERT_HEAD is `sha` (another revert in progress, the learning
    loop's rollback, isn't ours to abort), abort it, twice if need be, and check after each. A repo left with
    REVERT_HEAD and conflict markers in a live playbook is said so, naming the command."""
    for attempt in (1, 2, 3):
        if read._git(repo, "rev-parse", "-q", "--verify", "REVERT_HEAD").stdout.strip() != sha:
            return  # none in progress, or not this undo's
        if attempt < 3:
            read._git(repo, "revert", "--abort")
    raise Conflict("the undo failed and `git revert --abort` didn't finish: the repo needs attention, run "
                   "`git revert --abort` in it")


def _loads(repo: Path, files: list[str]) -> str | None:
    """The loader's refusal of a channel's settings.json among `files` (repo paths), as they are now; None when every
    one loads."""
    top = read._rel(repo, channels_root())
    for f in files:
        name = f.removeprefix(top + "/").partition("/")[0]
        if f == f"{top}/{name}/settings.json":
            try:
                load(name, need=())
            except ValueError as e:
                return str(e).partition("settings.json: ")[2] or str(e)
    return None


def undo(sha: str, *, repo: Path, root: Path) -> str:
    """git revert of one listed change. A conflict is aborted and refused. A revert that leaves a channel's settings the
    loader refuses (the loader changed since, an odd merge) is taken back by a revert of its own and refused with the
    loader's words, so no undo skips the settings check (final review, 10 Nov). A learning-loop change also gets a line
    in that loop's record. Returns the revert's commit."""
    found = next((c for c in read.changes(repo)["changes"] if c["sha"].startswith(sha)), None)
    if found is None:
        raise NotFound(f"no change {sha}")
    with _edits:
        r = read._git(repo, "revert", "--no-edit", found["sha"])
        if r.returncode:
            _abort_revert(repo, found["sha"])
            raise Conflict(f"can't undo this change: {(r.stderr or r.stdout).strip()}")
        done = _head(repo)
        if why := _loads(repo, found["files"]):
            back = read._git(repo, "revert", "--no-edit", done)  # this undo's own commit, whatever landed since
            if back.returncode:
                _abort_revert(repo, done)
                raise Conflict(f"the undo left settings the loader refuses ({why}), and git couldn't take it back: "
                               f"{(back.stderr or back.stdout).strip()}. Run `git revert {done}` in the repo")
            raise Invalid(why)
        if found["by"] == "loop" and found["channel"]:
            _note(root, found["channel"], {"at": datetime.now(timezone.utc).isoformat(), "status": "reverted",
                                           "by": "operator", "commit": found["sha"]})
        return done


def remove_topic(root: Path, brief_id: int) -> None:
    conn = store.connect(root)
    try:
        row = conn.execute("select status from briefs where id = ?", (brief_id,)).fetchone()
        if row is None:
            raise NotFound(f"no topic {brief_id}")
        if row["status"] != "queued":
            raise Conflict(f"topic {brief_id} is {row['status']}, not queued")
        store.mark(conn, brief_id, "removed")
    finally:
        conn.close()
