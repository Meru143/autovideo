"""The topic store (Plan 3a Task 1; design 5.4.1, 5.6; FAILURE_MODES.md, topics): queued briefs and published topics in
the factory's SQLite database, plus the tick lock. Every function takes its connection and its
"now", so the tick and the tests drive time explicitly."""
from __future__ import annotations

import contextlib
import json
import re
import sqlite3
from datetime import datetime
from pathlib import Path


from .. import OUT
from ..locks import held

SCHEMA = """
create table if not exists briefs (id integer primary key autoincrement, workspace text, key text, topic_key text,
    brief text, score real, timely integer, status text default 'queued', created_at text, updated_at text);
create table if not exists published_topics (workspace text, topic_key text, at text);
create table if not exists stories (workspace text, situation text, outcome text, job text, at text);
create table if not exists published_videos (workspace text, video_id text, job_id text, brief_id integer, lever text,
    topic_key text, at text, primary key (workspace, video_id));
create table if not exists video_stats (workspace text, video_id text, as_of text, age_days integer,
    live_views integer, live_likes integer, live_comments integer, views integer, engaged_views integer,
    avg_view_pct real, avg_view_s real, likes integer, shares integer, subs_gained integer,
    primary key (workspace, video_id, as_of));
"""
STOP = set("a an and are as at be but by for from has have how i if in into is it its of on or so that the their them "
           "then there they this to up was we what when where who why will with you your".split())


def connect(root: Path = OUT) -> sqlite3.Connection:
    """The factory database in WAL mode (readers never block the tick), with the topic tables created."""
    root.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(root / "factory.db", timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("pragma journal_mode=wal")
    conn.executescript(SCHEMA)
    return conn


def topic_key(text: str) -> str:
    """A topic's identity across wording: lowercase content words, crudely stemmed, unique and sorted."""
    words = (re.sub(r"(?:ing|ed|es|s)$", "", w) for w in re.findall(r"[a-z']+", text.lower()) if w not in STOP)
    return " ".join(sorted({w for w in words if len(w) > 1}))


def queue_brief(conn: sqlite3.Connection, workspace: str, brief: dict, now: datetime) -> int:
    """Queue one scored brief; returns its id."""
    cur = conn.execute("insert into briefs (workspace, key, topic_key, brief, score, timely, created_at, updated_at) "
                       "values (?, ?, ?, ?, ?, ?, ?, ?)",
                       (workspace, brief["key"], topic_key(brief["key"]), json.dumps(brief), float(brief["score"]),
                        int(bool(brief.get("timely"))), now.isoformat(), now.isoformat()))
    conn.commit()
    return cur.lastrowid


def effective_score(score: float, timely: bool, created_at: str, now: datetime, half_life_days: float) -> float:
    """A timely brief's score halves every half_life_days (design 5.6: a 14-day half-life); evergreen ones keep it."""
    if not timely:
        return score
    age_days = (now - datetime.fromisoformat(created_at)).total_seconds() / 86400
    return score * 0.5 ** (max(age_days, 0.0) / half_life_days)


def live(conn: sqlite3.Connection, workspace: str, now: datetime, *, min_score: float,
         half_life_days: float) -> list[dict]:
    """Queued briefs whose decayed score still clears min_score, best first."""
    found = []
    for r in conn.execute("select * from briefs where workspace = ? and status = 'queued'", (workspace,)):
        eff = effective_score(r["score"], bool(r["timely"]), r["created_at"], now, half_life_days)
        if eff >= min_score:
            found.append({**json.loads(r["brief"]), "id": r["id"], "topic_key": r["topic_key"],
                          "effective_score": round(eff, 1)})
    return sorted(found, key=lambda b: -b["effective_score"])


def best_brief(conn: sqlite3.Connection, workspace: str, now: datetime, *, min_score: float,
               half_life_days: float) -> dict | None:
    """The highest-scoring queued brief whose decayed score still clears min_score, or None."""
    found = live(conn, workspace, now, min_score=min_score, half_life_days=half_life_days)
    return found[0] if found else None


def expire(conn: sqlite3.Connection, workspace: str, now: datetime, *, min_score: float, half_life_days: float) -> int:
    """Mark queued briefs whose decayed score fell under min_score as expired; returns how many."""
    keep = {b["id"] for b in live(conn, workspace, now, min_score=min_score, half_life_days=half_life_days)}
    stale = [r["id"] for r in conn.execute("select id from briefs where workspace = ? and status = 'queued'",
                                            (workspace,)) if r["id"] not in keep]
    for brief_id in stale:
        mark(conn, brief_id, "expired", now)
    return len(stale)


def mark(conn: sqlite3.Connection, brief_id: int, status: str, now: datetime | None = None) -> None:
    conn.execute("update briefs set status = ?, updated_at = ? where id = ?",
                 (status, (now or datetime.now().astimezone()).isoformat(), brief_id))
    conn.commit()


def record_story(conn: sqlite3.Connection, workspace: str, situation: str, outcome: str, job: str,
                 at: datetime) -> None:
    """A story episode's (situation, outcome) pair, so no later episode repeats either (design 5.4.2)."""
    conn.execute("insert into stories values (?, ?, ?, ?, ?)", (workspace, situation, outcome, job, at.isoformat()))
    conn.commit()


def stories(conn: sqlite3.Connection, workspace: str) -> list[sqlite3.Row]:
    return list(conn.execute("select * from stories where workspace = ? order by at", (workspace,)))


def record_published(conn: sqlite3.Connection, workspace: str, key: str, at: datetime) -> None:
    conn.execute("insert into published_topics values (?, ?, ?)", (workspace, topic_key(key), at.isoformat()))
    conn.commit()


@contextlib.contextmanager
def tick_lock(root: Path = OUT):
    """Yields True when this process holds the tick lock, False when another tick holds it. The OS drops the lock when
    its holder ends, however it ends, so a lock file left by a crash or a power cut never stops the factory (9 Nov)."""
    with held(root / "tick_os.lock", wait=False) as got:
        yield got
