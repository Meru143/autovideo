"""The two checks before anything posts (Plan 1 Task 6; D21; FAILURE_MODES.md, prepublic). No human looks first
(operator, 7 Nov), so the video's own visible text is checked against the channel's off-limits words, and its script
against the channel's earlier videos. publish.guard refuses an upload without a passing prepublic.json."""
from __future__ import annotations

import html
import json
import re
from pathlib import Path

from .jobs import Job, Quarantined, _db
from .workspace import blocked, load

SCRIPT_MAX = 0.5  # [H] word-3-gram Jaccard of the narration at which a script is a near-duplicate
TITLE_MAX = 0.8  # [H] the same for titles, which are short and share more by chance
FINISHED = ("done", "uploaded_private")


STRING = re.compile(r'"(?:[^"\\\n]|\\.)*"' r"|'(?:[^'\\\n]|\\.)*'" r"|`(?:[^`\\]|\\.)*`")


def _strings(code: str) -> list[str]:
    """Every string literal in some script: words a script sets on screen (textContent, fillText, signs) live in them.
    ponytail: a word split by concatenation ("AD" + "HD") is not joined; a string that never reaches the screen is a
    false positive, the safe direction (the video waits)."""
    return [m[1:-1] for m in STRING.findall(code)]


def page_text(project: Path) -> str:
    """What the viewer can read: the pages' markup with inline tags removed so a word split across spans joins, plus
    every string literal in the pages' inline scripts and the project's own .js files (not vendor/, third-party)."""
    parts = []
    for f in [project / "index.html", *sorted((project / "compositions").glob("*.html"))]:
        if f.exists():
            raw = f.read_text(encoding="utf-8")
            for code in re.findall(r"<script\b[^>]*>(.*?)</script>", raw, flags=re.S | re.I):
                parts += _strings(code)
            t = re.sub(r"<(script|style)\b.*?</\1>", " ", raw, flags=re.S | re.I)
            t = re.sub(r"</?(span|tspan|b|i|em|strong|u|mark|small)\b[^>]*>", "", t, flags=re.I)
            parts.append(html.unescape(re.sub(r"<[^>]*>", " ", t)))
    for js in sorted(project.rglob("*.js")):
        if "vendor" not in js.relative_to(project).parts:
            parts += _strings(js.read_text(encoding="utf-8"))
    return "\n".join(parts)


def offlimits(job: Job, ws: dict) -> list[str]:
    return blocked(page_text(job.dir / "project"), ws["blocked_words"] + ws.get("canon_blocklist", []))


def _grams(text: str) -> set[tuple[str, ...]]:
    w = re.findall(r"[a-z0-9']+", text.lower().translate(str.maketrans("‘’", "''")))
    return {tuple(w[i:i + 3]) for i in range(len(w) - 2)}


def _jaccard(a: str, b: str) -> float:
    x, y = _grams(a), _grams(b)
    return len(x & y) / len(x | y) if x | y else 0.0


def _read(job_dir: Path) -> dict[str, str]:
    n, m = job_dir / "narration.txt", job_dir / "metadata.json"
    return {"narration": n.read_text(encoding="utf-8") if n.exists() else "",
            "title": json.loads(m.read_text(encoding="utf-8")).get("title", "") if m.exists() else ""}


def similar(job: Job, ws: dict, root: Path) -> list[dict]:
    """Earlier finished videos of this channel that the job's narration or title nearly repeats. The job itself is
    excluded, so a reworked cut of it never matches its own earlier one."""
    with _db(root) as c:
        mine = c.execute("select created_at from jobs where id = ?", (job.id,)).fetchone()[0]
        rows = c.execute("select id from jobs where workspace = ? and id != ? and created_at < ? and state in (?, ?)",
                         (job.workspace, job.id, mine, *FINISHED)).fetchall()
    now, hits = _read(job.dir), []
    for (jid,) in rows:
        before = _read(root / "jobs" / jid)
        for field, limit in (("narration", SCRIPT_MAX), ("title", TITLE_MAX)):
            score = _jaccard(now[field], before[field])
            if score >= limit:
                hits.append({"job": jid, "field": field, "score": round(score, 2)})
    return hits


def prepublic(job: Job) -> dict:
    ws = load(job.workspace)
    result = {"words": offlimits(job, ws), "similar": similar(job, ws, job.root)}
    result["ok"] = not result["words"] and not result["similar"]
    (job.dir / "prepublic.json").write_text(json.dumps(result, indent=1), encoding="utf-8")
    if not result["ok"]:
        raise Quarantined(f"prepublic refused: off-limits words {result['words']}; near-duplicates {result['similar']}")
    return result
