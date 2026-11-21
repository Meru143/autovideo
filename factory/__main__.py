"""python -m factory <command>  (run from the repo root with .venv)"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .workspace import channels


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):  # tool output carries symbols a cp1252 pipe can't encode
        stream.reconfigure(errors="replace")
    ap = argparse.ArgumentParser(prog="factory")
    sub = ap.add_subparsers(dest="cmd", required=True)
    st = sub.add_parser("selftest", help="E2E selftest; writes factory/results/e2e/<stamp>-selftest.json")
    st.add_argument("--stages", default=None, help="comma-separated stage names (default: all)")
    sub.add_parser("lint", help="AI-sounding writing in a script; exit 1 on any finding").add_argument("script", type=Path)
    sub.add_parser("still", help="still spans over 1.5 s (closing hold up to 3 s); exit 1 on any").add_argument(
        "video", type=Path)
    vo = sub.add_parser("voice", help="narrate a script for a brand and check it (one Gemini request for explainers)")
    vo.add_argument("brand", choices=channels())  # every channel folder with a settings.json
    vo.add_argument("script", type=Path)
    vo.add_argument("--out", type=Path, required=True)
    jb = sub.add_parser("job", help="create or inspect a job (job run arrives with the pipeline, Plan 2b Task 12)")
    jsub = jb.add_subparsers(dest="job_cmd", required=True)
    jn = jsub.add_parser("new", help="start a job from a brief; prints its ID")
    jn.add_argument("workspace")
    jn.add_argument("brief", type=Path)
    jsub.add_parser("show", help="print a job's state and finished stages").add_argument("job_id")
    jr = jsub.add_parser("run", help="run a job's unfinished stages in order (resumes after a stop)")
    jr.add_argument("job_id")
    jr.add_argument("--until", default=None, help="stop after this stage (e.g. scene: render later)")
    js = jsub.add_parser("reset", help="reopen a quarantined (or any) job, logged in its operator.jsonl")
    js.add_argument("job_id")
    js.add_argument("--budget", action="store_true", help="also restore the regeneration budget")
    sub.add_parser("loop", help="the learning loop's weekly step for one channel (Plan 3b)").add_argument("channel")
    sub.add_parser("tick", help="one scheduler tick: publish at a due slot, one job, topics (Plan 3a Task 6); Task "
                               "Scheduler runs it every 15 minutes")
    sub.add_parser("backlog", help="briefs, finished videos and next slots per channel").add_subparsers(
        dest="backlog_cmd", required=True).add_parser("show", help="print the backlog")
    kit_sub = sub.add_parser("kit", help="a story channel's kit (story plan Tasks 3-5)").add_subparsers(
        dest="kit_cmd", required=True)
    kt = kit_sub.add_parser("inventory", help="what the given jobs' shot tables use: writes brand/inventory.json and "
                                              "prints the short list for the operator's OK")
    kt.add_argument("workspace")
    kt.add_argument("--jobs", nargs="+", required=True, help="the approved stories' job ids")
    kb = kit_sub.add_parser("build", help="write, check and install brand/cast.js or sets.js for the inventory (a "
                                          "writer run; the test key while building), then print where its stills are "
                                          "for the operator's look approval")
    kb.add_argument("workspace")
    kb.add_argument("kind", choices=("cast", "sets"))
    cs = sub.add_parser("console", help="the operator console: a local page for the factory (127.0.0.1 only); prints "
                                        "its address and opens it")
    cs.add_argument("--port", type=int, default=0, help="default 0: the OS picks a free port")
    cs.add_argument("--no-browser", action="store_true")
    cs.add_argument("--fixture", type=Path, default=None, help="serve a scratch factory built in this folder, with "
                                                               "fake publish legs (for tests)")
    cs.add_argument("--key", default=None, help="the launch key, with --fixture only (else a new one every launch)")
    cl = sub.add_parser("clip", help="VOD and live-stream clipping commands").add_subparsers(
        dest="clip_cmd", required=True)
    ci = cl.add_parser("ingest", help="download a Twitch VOD/YouTube/local video, score highlights, and queue clip jobs")
    ci.add_argument("channel")
    ci.add_argument("url")
    ci.add_argument("--num-clips", type=int, default=None)
    ci.add_argument("--layout", default=None, choices=("auto", "fill", "fit"))
    ci.add_argument("--caption-style", default=None)
    ci.add_argument("--provider", default=None, help="LLM provider override (e.g. claude, gemini, openai, or custom provider in secrets/providers.json)")
    ci.add_argument("--model", default=None, help="Model override for provider")
    ci.add_argument("--long-vod", action=argparse.BooleanOptionalAction, default=None, help="Force or disable audio-first + targeted 1080p section downloads (auto-enabled for VODs > 2h)")
    ci.add_argument("--ingest-id", default=None, help="Optional ingest tracking ID for Console Home progress")
    cr = cl.add_parser("radar", help="find top live Twitch/YouTube streams and fastest-rising videos (keyless)")
    cr.add_argument("--query", required=True, help="Game or topic query to scan")
    cr.add_argument("--mode", default="all", choices=("streams", "trends", "all"))
    cr.add_argument("--limit", type=int, default=15)
    cr.add_argument("--hours", type=float, default=24.0, help="Lookback window in hours for trending videos")
    cr.add_argument("--json", action="store_true", dest="as_json", help="Print raw JSON instead of a table")
    clv = cl.add_parser("live", help="record a live Twitch/YouTube stream and auto-cut clips on chat spikes")
    clv.add_argument("url", help="Live Twitch (https://twitch.tv/<login>) or YouTube live URL")
    clv.add_argument("--channel", default=None, help="Optional clip channel name to auto-queue spike jobs")
    clv.add_argument("--minutes", type=float, default=60.0)
    clv.add_argument("--max-height", type=int, default=1080)
    clv.add_argument("--before", type=float, default=15.0)
    clv.add_argument("--after", type=float, default=20.0)
    clv.add_argument("--ratio", type=float, default=1.8, help="Chat spike multiplier over rolling baseline")
    clv.add_argument("--min-msgs", type=int, default=8, help="Minimum messages in window to trigger a spike")
    clv.add_argument("--cooldown", type=float, default=45.0)
    clv.add_argument("--lag", type=float, default=8.0)
    clv.add_argument("--no-queue", action="store_true", help="Save spike_*.mp4 files without queuing autovideo jobs")
    clv.add_argument("--watch-id", default=None, help="Optional live watcher tracking ID for Console Home")
    a = ap.parse_args(argv)
    if a.cmd == "selftest":
        try:
            from . import selftest
        except ImportError:
            print("selftest is not included in this distribution.", file=sys.stderr)
            return 1
        return selftest.run(a.stages.split(",") if a.stages else None, ["python", "-m", "factory", *sys.argv[1:]])
    if a.cmd == "console":
        import os
        import re
        import secrets

        from . import OUT, ROOT
        from .console import server
        if a.key is not None and (a.fixture is None or not re.fullmatch(r"[A-Za-z0-9_-]+", a.key)):
            ap.error("--key goes with --fixture only, and takes letters, digits, - and _")
        root, repo, legs, key = OUT, ROOT, None, a.key or secrets.token_urlsafe(24)
        if a.fixture:
            from .console import fixture
            f = fixture.build(a.fixture)
            os.environ["FACTORY_WORKSPACES"], os.environ["FACTORY_CLAUDE_CONFIG"] = (
                str(f["channels_root"]), str(f["claude_config"]))
            os.environ["FACTORY_SETTINGS"] = str(f["settings"])  # saves commit in the scratch repo, never the live file
            root, repo, legs = f["root"], f["repo"], fixture.legs(f["root"])
        try:
            server.serve(root, repo=repo, key=key, port=a.port, legs=legs, open_browser=not a.no_browser)
        except OSError as e:  # a taken port: say so, rather than a traceback
            print(f"console: cannot listen on port {a.port}: {e}")
            return 1
        return 0
    if a.cmd == "lint":
        from .script_lint import lint
        found = lint(a.script.read_text(encoding="utf-8"))
        for f in found:
            print(f"{f['rule']}: {f['evidence']}")
        return 1 if found else 0
    if a.cmd == "still":
        from .stillness import check
        found = check(a.video)
        for f in found:
            print(f"{f['start']}-{f['end']} s ({f['seconds']} s): {f['why']}")
        return 1 if found else 0
    if a.cmd == "voice":
        from .voice import synthesize
        try:
            r = synthesize(a.brand, a.script.read_text(encoding="utf-8"), a.out)
        except RuntimeError as e:  # VoiceCheckFailed and QuotaExhausted included; voice.json keeps the numbers
            print(f"voice {a.brand}: {type(e).__name__}: {e}")
            return 1
        print(f"voice {a.brand}: {r['seconds']} s, WER {r['wer']:.1%}, {r['rate']} {r['rate_unit']} -> {a.out}")
        return 0
    if a.cmd == "job":
        import json

        from . import jobs
        from .workspace import load
        if a.job_cmd == "new":
            load(a.workspace)  # refuse a broken workspace before a job exists
            print(jobs.new(a.workspace, a.brief))
            return 0
        if a.job_cmd == "reset":
            print(f"job {a.job_id}: {jobs.reset(a.job_id, budget=a.budget)}")
            return 0
        if a.job_cmd == "run":
            from .pipeline import run_job
            state = run_job(a.job_id, a.until)
            print(f"job {a.job_id}: {state}")
            return 0 if state in ("done", "uploaded_private") else 1
        print(json.dumps(jobs.show(a.job_id), indent=2, default=str))
        return 0
    if a.cmd in ("tick", "backlog"):
        import json

        from . import scheduler
        report = scheduler.backlog() if a.cmd == "backlog" else scheduler.tick()
        if a.cmd == "tick" and not report.get("busy"):
            from . import stats  # each public video's numbers, once a day per channel (9 Nov); never breaks the tick
            try:
                if pulled := stats.daily():
                    report["stats"] = pulled
            except Exception as e:  # the tick already ran; the report says why the pull didn't
                report["stats"] = f"{type(e).__name__}: {str(e)[:200]}"
            from .loop.apply import weekly  # the learning loop's step, once a week per channel, in its own process
            try:
                if started := weekly():
                    report["loop"] = started
            except Exception as e:
                report["loop"] = f"{type(e).__name__}: {str(e)[:200]}"
        print(json.dumps(report, indent=2, default=str))
        return 0
    if a.cmd == "loop":
        import json
        from datetime import datetime, timezone

        from .loop.apply import step
        from .topics import store
        from .workspace import load
        conn = store.connect()
        try:
            print(json.dumps(step(conn, load(a.channel, need=()), datetime.now(timezone.utc)), indent=2, default=str))
        finally:
            conn.close()
        return 0
    if a.cmd == "kit":
        import json
        from pathlib import Path as P

        from . import OUT
        from .kit import inventory, summary
        from .workspace import load
        ws = load(a.workspace)
        if a.kit_cmd == "build":
            from .kit_build import build
            r = build(ws, a.kind)
            print(json.dumps({k: v for k, v in r.items() if k != "findings"} | {"rounds_findings": r["findings"]},
                             indent=2, default=str))
            return 0
        tables = [json.loads((OUT / "jobs" / j / "shot_table.json").read_text(encoding="utf-8")) for j in a.jobs]
        inv = {"jobs": a.jobs, **inventory([t["shots"] if isinstance(t, dict) else t for t in tables])}
        out = P(ws["dir"]) / "brand" / "inventory.json"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(inv, indent=2), encoding="utf-8")
        print(f"{a.workspace} kit needs: {summary(inv)} -> {out}")
        return 0
    if a.cmd == "clip" and a.clip_cmd == "ingest":
        import json
        from .clip.pipeline import ingest_vod, update_ingest_status
        try:
            created = ingest_vod(
                a.channel,
                a.url,
                num_clips=a.num_clips,
                layout=a.layout,
                caption_style=a.caption_style,
                provider=a.provider,
                model=a.model,
                long_vod=a.long_vod,
                ingest_id=a.ingest_id,
            )
        except Exception as e:
            if a.ingest_id:
                update_ingest_status(a.ingest_id, channel=a.channel, url=a.url, stage="error",
                                     detail=str(e)[:240], error=f"{type(e).__name__}: {str(e)[:240]}")
            raise
        print(json.dumps({"queued": created}, indent=2))
        return 0
    if a.cmd == "clip" and a.clip_cmd == "radar":
        import json
        from .clip.radar import format_radar_table, scan_radar
        data = scan_radar(query=a.query, mode=a.mode, limit=a.limit, hours=a.hours)
        print(json.dumps(data, indent=2) if a.as_json else format_radar_table(data))
        return 0
    if a.cmd == "clip" and a.clip_cmd == "live":
        import json
        from .clip.live import update_live_status, watch_live
        try:
            summary = watch_live(
                a.url,
                channel=a.channel,
                minutes=a.minutes,
                max_height=a.max_height,
                before=a.before,
                after=a.after,
                ratio=a.ratio,
                min_msgs=a.min_msgs,
                cooldown=a.cooldown,
                lag=a.lag,
                auto_queue=not a.no_queue,
                watch_id=a.watch_id,
            )
        except Exception as e:
            if a.watch_id:
                update_live_status(
                    a.watch_id, channel=a.channel, url=a.url, stage="error",
                    detail=str(e)[:240], error=f"{type(e).__name__}: {str(e)[:240]}",
                )
            raise
        print(json.dumps(summary, indent=2))
        return 0
    return 2


if __name__ == "__main__":
    sys.exit(main())
