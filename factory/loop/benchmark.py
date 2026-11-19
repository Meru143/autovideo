"""The loop's benchmark (Plan 3b Task 4): the proposed change runs against the current version on the same inputs, and a
blind judge picks per pair, in an order drawn from the change and recorded. A script change runs the script stage on up
to 3 recent videos' own briefs and fetched sources per side, the best and worst first (ruling 6); a topics change runs
one topic pass per side. The change wins with 2 of 3 pairs, or the one pair for topics. Settings that won't load, a
failure on either side, or a judge that can't answer, is a loss (FAILURE_MODES.md, loop)."""
from __future__ import annotations

import contextlib
import hashlib
import json
import os
import random
import shutil
from datetime import datetime, timezone
from pathlib import Path

from .. import OUT
from ..claude_call import SENSOR, call
from ..workspace import channels_root, load

JUDGE = Path(__file__).with_name("judge.md")  # the judge's guardrail: never in the loop's allowlist
VERDICT = {"type": "object", "required": ["winner", "why"], "properties": {
    "winner": {"type": "string", "enum": ["A", "B"]}, "why": {"type": "string"}}}


def kind(proposal: dict) -> str:
    path = str(proposal["path"])
    return "topics" if path.startswith("topics.") or Path(path).stem == "topics" else "script"


def changed_copy(ws: dict, proposal: dict, dst: Path) -> Path:
    """A channels folder holding only this channel, with the change applied; returns its root."""
    out = dst / ws["name"]
    shutil.rmtree(dst, ignore_errors=True)
    shutil.copytree(Path(ws["dir"]), out)
    if proposal["target"] == "playbook":
        (out / ws["playbooks"][Path(proposal["path"]).stem]).write_text(proposal["change"], encoding="utf-8")
    else:
        settings = json.loads((out / "settings.json").read_text(encoding="utf-8-sig"))
        *head, last = str(proposal["path"]).split(".")
        node = settings
        for k in head:
            node = node[k]
        node[last] = proposal["change"]
        (out / "settings.json").write_text(json.dumps(settings, indent=2), encoding="utf-8")
    return dst


@contextlib.contextmanager
def _workspaces(root: Path):
    saved = os.environ.get("FACTORY_WORKSPACES")
    os.environ["FACTORY_WORKSPACES"] = str(root)
    try:
        yield
    finally:
        os.environ.pop("FACTORY_WORKSPACES", None) if saved is None else os.environ.__setitem__("FACTORY_WORKSPACES", saved)


def items(root: Path, videos: list[dict]) -> list[dict]:
    """Up to 3 videos whose jobs kept their brief and fetched sources, the best and worst first."""
    order = [*videos[:1], *videos[-1:], *videos[1:-1]] if videos else []
    out = []
    for v in order:
        d = root / "jobs" / str(v.get("job"))
        if (d / "sources" / "index.json").exists() and (d / "brief.md").exists() and d.name not in [i["job"] for i in out]:
            out.append({"job": d.name, "brief": d / "brief.md", "sources": d / "sources"})
    return out[:3]


def stage_runner(claude_bin, sensor):
    """The production runner: the script stage on an item, or one topic pass, on the channel under ws_root."""
    def run(ws_root: Path, name: str, item, scratch: Path) -> str:
        from .. import jobs
        scratch.mkdir(parents=True, exist_ok=True)
        with _workspaces(ws_root):
            if item is None:  # a topics change: one pass into a scratch store
                from ..topics import briefs, store
                conn = store.connect(scratch)
                try:
                    r = briefs.write_briefs(conn, load(name), datetime.now(timezone.utc), claude_bin=claude_bin,
                                            sensor=sensor, root=scratch)
                    rows = conn.execute("select brief from briefs where workspace = ?", (name,)).fetchall()
                finally:
                    conn.close()
                if not rows:
                    raise RuntimeError(f"the topic pass queued nothing: {r.get('skipped') or r.get('rejected')}")
                return "\n\n".join(row[0] for row in rows)
            from ..stages.script import script
            job = jobs.load(jobs.new(name, item["brief"], root=scratch), root=scratch)
            shutil.copytree(item["sources"], job.dir / "sources")
            script(job, claude_bin=claude_bin, sensor=sensor)
            return (job.dir / "narration.txt").read_text(encoding="utf-8")
    return run


def _judge(ws: dict, what: str, a: str, b: str, cwd: Path, claude_bin, sensor) -> str | None:
    cwd.mkdir(parents=True, exist_ok=True)
    prompt = (f"Two {what} for {ws.get('display_name', ws['name'])}.\n\n## A\n{a}\n\n## B\n{b}\n\n"
              "Which would more viewers watch to the end? Reply with JSON: winner (A or B) and why.")
    try:
        r = call("judge", prompt, cwd=cwd, tools=(), schema=VERDICT, system_file=JUDGE, claude_bin=claude_bin,
                 sensor=sensor)
    except Exception:  # refused, a quota wall, a crash: no verdict, so no change
        return None
    winner = r.data.get("winner") if isinstance(r.data, dict) and not r.isolation else None
    return winner if winner in ("A", "B") else None


def benchmark(ws: dict, proposal: dict, targets: list, *, run=None, claude_bin: list[str] | None = None,
              sensor: Path = SENSOR, root: Path = OUT) -> dict:
    """{verdict: win | loss, wins, losses, pairs, seed, why?}. targets are items() for a script change; a topics
    change ignores them and runs one pair."""
    k = kind(proposal)
    seed = int(hashlib.sha256(json.dumps(proposal, sort_keys=True).encode()).hexdigest()[:8], 16)
    rng, work = random.Random(seed), root / "loop" / ws["name"] / f"bench-{seed:08x}"
    shutil.rmtree(work, ignore_errors=True)
    new_root = changed_copy(ws, proposal, work / "channels")
    try:
        load(ws["name"], root=new_root)
    except ValueError as e:
        return {"verdict": "loss", "wins": 0, "losses": 1, "pairs": [], "seed": seed,
                "why": f"settings that won't load: {str(e)[:300]}"}
    run = run or stage_runner(claude_bin, sensor)
    pairs = []
    for i, item in enumerate([None] if k == "topics" else targets[:3]):
        try:
            old = run(channels_root(), ws["name"], item, work / f"old{i}")
            new = run(new_root, ws["name"], item, work / f"new{i}")
        except Exception as e:  # no fair pair: the change doesn't win it
            pairs.append({"item": i, "error": f"{type(e).__name__}: {str(e)[:200]}", "judge": None, "winner": "old"})
            continue
        new_first = rng.random() < 0.5
        a, b = (new, old) if new_first else (old, new)
        said = _judge(ws, "sets of video ideas" if k == "topics" else "narration scripts for the same video", a, b,
                      work / f"judge{i}", claude_bin, sensor)
        pairs.append({"item": i, "new_was": "A" if new_first else "B", "judge": said,
                      "winner": "new" if said and (said == "A") == new_first else "old"})
    wins = sum(p["winner"] == "new" for p in pairs)
    if not pairs:
        return {"verdict": "loss", "wins": 0, "losses": 0, "pairs": [], "seed": seed, "why": "nothing to run it on"}
    return {"verdict": "win" if wins >= (1 if k == "topics" else 2) else "loss", "wins": wins,
            "losses": len(pairs) - wins, "pairs": pairs, "seed": seed}
