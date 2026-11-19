"""Isolated `claude -p` calls for the factory's creative stages (design 5.5.3, 6c, 8.5 item 8; Plan 2b Task 3;
FAILURE_MODES.md, claude_call). Ported from spikes/bakeoff/bakeoff/author.py: the same isolation, effort and billing
guards. Claude never uploads, never sees credentials and never decides that a video ships: callers check its output."""
from __future__ import annotations

import json
import os
import shutil
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from . import OUT, ROOT, settings
from .procs import run_cli

# Each step's model and effort, and the stop points, are data: factory/settings.json (settings.py), read at every call.
# No wall clock on a Claude run (operator, 6 Nov: "no need to keep 60 min limit, it keeps on happening"): a run is
# stopped only after STALL_S with no output at all. stream-json prints an event per step, and a writer composing one
# big file went 15 minutes quiet (6 Nov), so 45 minutes of silence means a hang.
STALL_S = 2700
STATUS_LINE = Path.home() / ".claude" / "rate-limits.json"  # the interactive status line: the API-mode guard only
SENSOR = None  # None: the factory's own reading, through its own sign-in (Plan 3a Task 6; no open window needed)
# A limit hit mid-call pauses the job (QuotaWait). ponytail: a long call can still cross a model's own bar mid-run;
# that's a QuotaWait too, and the retry falls back (settings.py, stop_at.model_weekly).
SENSOR_MAX_AGE_S, USAGE_MAX_AGE_S = 900, 300
HARDENED = {"HYPERFRAMES_NO_TELEMETRY": "1", "DO_NOT_TRACK": "1", "HYPERFRAMES_NO_UPDATE_CHECK": "1",
            "HYPERFRAMES_SKIP_SKILLS": "1", "HYPERFRAMES_NO_AUTO_INSTALL": "1",
            "PRODUCER_EXPERIMENTAL_FAST_CAPTURE": "false", "HF_DE_PARALLEL_ROUTER": "false"}  # design 6a


class QuotaWait(RuntimeError):
    """A quota wall: the job waits (waiting_quota) instead of failing, and the regeneration that paid for the call comes
    back (final review, Important 1)."""


class Refused(QuotaWait):
    """The call was refused before anything spawned (soft cap, stale sensor, or a billing mismatch): no quota was spent."""


LIMIT_TEXT = "You've hit your"  # "... session limit · resets 3:45pm", "... weekly limit", "... monthly spend limit" (6c)
QUOTA_RETRIES = {"rate_limit", "billing_error"}  # api_retry categories that mean the account, not the request


@dataclass
class CallResult:
    stage: str
    exit_code: int
    timed_out: bool
    seconds: float
    cost_usd: float | None = None
    turns: int | None = None
    text: str = ""
    data: dict | None = None
    billing: str = "subscription"
    init: dict | None = None
    isolation: list[str] = field(default_factory=list)
    event_types: list[str] = field(default_factory=list)
    ok: bool = False  # a result event with subtype success and no error
    limit_text: str | None = None
    api_retry: list[str] = field(default_factory=list)


def claude_env(config_dir: Path | None = None) -> dict[str, str]:
    """The child's env: no parent Claude Code or Orca variables, plus the renderer hardening (design 6a). config_dir
    picks a login: the factory's own subscription sign-in, where no API key may outrank it."""
    env = {k: v for k, v in os.environ.items() if not k.upper().startswith(("CLAUDE", "ORCA_"))}
    if config_dir is not None:
        env = {k: v for k, v in env.items() if k not in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN")}
        env["CLAUDE_CONFIG_DIR"] = str(config_dir)
    return {**env, **HARDENED}


def _api_key() -> str:
    """The test-credit key from secrets/anthropic_api.env (operator, 5 Nov): API test mode only, never printed."""
    f = Path(os.environ.get("FACTORY_API_KEY_FILE") or ROOT / "secrets" / "anthropic_api.env")
    lines = f.read_text(encoding="utf-8").splitlines() if f.exists() else []
    return next((v.strip() for k, _, v in (ln.partition("=") for ln in lines) if k.strip() == "ANTHROPIC_API_KEY"), "")


def _factory_config() -> Path:
    return Path(os.environ.get("FACTORY_CLAUDE_CONFIG") or Path.home() / ".claude-factory")


def refresh_quota(claude_bin: list[str] | None = None) -> Path:
    """The factory's own quota reading (FAILURE_MODES.md, quota sensor): `claude -p /usage` through its own sign-in,
    a local command with no model turn, cached USAGE_MAX_AGE_S in the status-line file's shape. A failed probe writes
    nothing, so a missing reading refuses instead of guessing."""
    path = Path(os.environ.get("FACTORY_QUOTA_FILE") or OUT / "quota.json")
    if path.exists() and time.time() - path.stat().st_mtime < USAGE_MAX_AGE_S:
        return path
    cmd = (claude_bin or [shutil.which("claude") or "claude"]) + [
        "-p", "--output-format", "stream-json", "--verbose", "--model", "claude-haiku-5-5", "--max-turns", "1",
        "--tools", "", "--safe-mode"]  # haiku only as a floor, in case a CLI ever sends /usage to a model
    proc = run_cli(cmd, env=claude_env(_factory_config()), timeout_s=120, input=b"/usage")
    for line in (proc.stdout or "").splitlines():
        try:
            lims = json.loads(line)["usage_report"]["rate_limits"]["limits"]
            by = {x["kind"]: x for x in lims}
            limits = {"five_hour": by["session"], "seven_day": by["weekly_all"]}
            for x in lims:  # a model's own weekly bar, e.g. Fable's on the Pro plan (10 Nov) -> seven_day_fable
                name = ((x.get("scope") or {}).get("model") or {}).get("display_name")
                if x["kind"] == "weekly_scoped" and name:
                    limits[f"seven_day_{name.lower()}"] = x
            reading = {"saved_at": datetime.now(timezone.utc).isoformat(), "rate_limits": {
                k: {"used_percentage": float(v["percent"]), "resets_at": v.get("resets_at")} for k, v in limits.items()}}
        except (ValueError, KeyError, TypeError):
            continue
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(reading), encoding="utf-8")
        break
    return path


def _capacity(sensor: Path) -> dict | None:
    """A fresh subscription reading {weekly, five_hour, and any model's own weekly bar by name, e.g. fable}, or None
    (no file, stale, or no rate limits: the API login)."""
    try:
        j = json.loads(sensor.read_text(encoding="utf-8"))
        rl = j["rate_limits"]
        age = (datetime.now(timezone.utc) - datetime.fromisoformat(j["saved_at"].replace("Z", "+00:00"))).total_seconds()
        reading = {"weekly": float(rl["seven_day"]["used_percentage"]), "five_hour": float(rl["five_hour"]["used_percentage"]),
                   **{k.removeprefix("seven_day_"): float(v["used_percentage"]) for k, v in rl.items()
                      if k.startswith("seven_day_")}}
    except (OSError, ValueError, KeyError, TypeError):
        return None
    return reading if -180 <= age <= SENSOR_MAX_AGE_S else None


def isolation_problems(init: dict, tools: list[str], key_source: str = "none") -> list[str]:
    """What a run loaded beyond its own tools, read from the system/init event. cc-plugin-* ship inside Claude Code."""
    def names(key: str) -> list[str]:
        return sorted(x.get("name", "?") if isinstance(x, dict) else str(x) for x in init.get(key) or [])
    problems = []
    if names("tools") != sorted(tools):
        problems.append(f"tools {names('tools')}, expected {sorted(tools)}")
    if names("mcp_servers"):
        problems.append(f"MCP servers {names('mcp_servers')}")
    if extra := [p for p in names("plugins") if not p.startswith("cc-plugin-")]:
        problems.append(f"plugins {extra}")
    if init.get("memory_paths"):
        problems.append("auto-memory on")
    if init.get("apiKeySource") != key_source:  # OAuth, or the test key we passed; anything else bills an unknown org
        problems.append(f"auth via {init.get('apiKeySource')!r}, not the operator's login")
    return problems


def call(stage: str, prompt: str, *, cwd: Path, tools: tuple[str, ...] = (), schema: dict | None = None,
         system_file: Path | None = None, add_dirs: tuple[Path, ...] = (), claude_bin: list[str] | None = None,
         usage_log: Path | None = None, sensor: Path = SENSOR, allow: tuple[str, ...] = (),
         transcript: Path | None = None) -> CallResult:
    """allow: Bash(<exact command>:*) patterns, the only commands a Bash tool may run; transcript: the raw stream."""
    kind = stage.split(":")[-1]
    try:
        s = settings.load()
    except ValueError as e:  # a hand edit the console would have refused: the job waits instead of failing
        raise Refused(f"{stage}: the factory settings don't load: {e}") from None
    if (step := s["steps"].get(kind)) is None:
        raise ValueError(f"{stage}: factory/settings.json sets no model and effort for this step")
    model, effort, stop, fallback = step["model"], step["effort"], s["stop_at"], s["fallback"]
    if "Bash" in tools and not allow:
        raise ValueError(f"{stage}: Bash needs its allowlist of exact commands; there is no open terminal")
    allowed = [t for t in tools if t != "Bash"] + list(allow)
    api, config, key = os.environ.get("FACTORY_BILLING") == "api", None, ""
    if sensor is None:  # production and real test runs: no window needed (Plan 3a Task 6)
        if api:  # the test credit through its own key, so no OAuth sign-in can collide (operator, 5 Nov)
            if not (key := _api_key()):
                raise Refused(f"{stage}: API test mode needs ANTHROPIC_API_KEY in secrets/anthropic_api.env")
        else:
            config = _factory_config()
            if not config.is_dir():
                raise Refused(f"{stage}: the factory isn't signed in to the subscription; run "
                              f"CLAUDE_CONFIG_DIR={config} claude and sign in")
            sensor = refresh_quota(claude_bin)
    cap = _capacity(sensor) if sensor is not None else None
    if api and cap is not None:
        raise Refused(f"{stage}: FACTORY_BILLING=api, but the sensor shows a subscription login")
    if not api:
        if cap is None:
            raise Refused(f"{stage}: no fresh quota sensor reading")
        if cap["weekly"] >= stop["weekly"] or cap["five_hour"] >= stop["five_hour"]:
            raise Refused(f"{stage}: soft cap (weekly {cap['weekly']:.0f}%, 5-hour {cap['five_hour']:.0f}%)")
    if cap is not None and model != fallback["model"] and cap.get(model.split("-")[1], 0.0) >= stop["model_weekly"]:
        model, effort = fallback["model"], fallback["effort"]  # that model's own weekly bar is nearly full: no wait
    # acceptEdits let a Bash `echo > file` through on the real CLI (5 Nov), so a call with Bash runs in dontAsk: only
    # the allowlist runs, everything else is denied without a prompt. Code stages without Bash keep acceptEdits.
    mode = "dontAsk" if "Bash" in tools else "acceptEdits"
    cmd = ((claude_bin or [shutil.which("claude") or "claude"])
           + ["-p", "--output-format", "stream-json", "--verbose", "--model", model, "--safe-mode", "--restricted",
              "--tools", ",".join(tools), "--permission-mode", mode, "--max-turns", "40", "--effort", effort]
           + (["--allowedTools", ",".join(allowed)] if allowed else [])
           + (["--json-schema", json.dumps(schema)] if schema else [])
           + (["--append-system-prompt-file", str(system_file)] if system_file else [])
           + [a for d in add_dirs for a in ("--add-dir", str(d))])
    env = {**claude_env(config), **({"ANTHROPIC_API_KEY": key} if key else {})}
    proc = run_cli(cmd, cwd=cwd, env=env, timeout_s=None, stall_s=STALL_S, input=prompt.encode("utf-8"))
    out, err, timed_out = proc.stdout, proc.stderr, proc.timed_out
    if transcript is not None:
        transcript.write_text(out, encoding="utf-8")
    r = CallResult(stage, proc.returncode, timed_out, proc.seconds)
    expected = list(tools) + (["StructuredOutput"] if schema else [])
    for line in (out + "\n" + err).splitlines():
        try:
            ev = json.loads(line)
        except ValueError:  # the CLI's own plain-text lines; the model's words only ever arrive inside JSON
            if r.limit_text is None and LIMIT_TEXT in line:
                r.limit_text = line.strip()[:200]
            continue
        if not isinstance(ev, dict) or not ev.get("type"):
            continue
        if ev["type"] not in r.event_types:
            r.event_types.append(ev["type"])
        if ev["type"] == "system" and ev.get("subtype") == "init" and r.init is None:
            r.isolation = isolation_problems(ev, expected, "ANTHROPIC_API_KEY" if key else "none")
            r.init = {k: ev.get(k) for k in ("claude_code_version", "model", "tools", "plugins", "apiKeySource")}
        elif ev["type"] == "result":
            r.cost_usd, r.turns, r.text = ev.get("total_cost_usd"), ev.get("num_turns"), str(ev.get("result") or "")
            r.ok = ev.get("subtype") == "success" and not ev.get("is_error")
            if not r.ok and r.limit_text is None and LIMIT_TEXT in r.text:
                r.limit_text = r.text.strip()[:200]
            if schema and isinstance(ev.get("structured_output"), dict):
                r.data = ev["structured_output"]
        if ev["type"] == "api_retry" or ev.get("subtype") == "api_retry":
            e = ev.get("error") if isinstance(ev.get("error"), dict) else {}
            if category := e.get("category") or ev.get("category") or ev.get("error_category"):
                r.api_retry.append(category)
    if api and "rate_limit_event" not in r.event_types:
        r.billing = "api"
    elif api:  # only subscription runs report the plan's rate limits
        r.isolation.append("billed the subscription (rate_limit_event), not API credit")
    log = usage_log or OUT / "usage.jsonl"
    log.parent.mkdir(parents=True, exist_ok=True)
    with log.open("a", encoding="utf-8") as f:
        f.write(json.dumps({"at": datetime.now(timezone.utc).isoformat(timespec="seconds"), "stage": stage,
                            "model": model, "cost_usd": r.cost_usd, "seconds": r.seconds, "turns": r.turns, "billing": r.billing,
                            "exit": r.exit_code, "timed_out": timed_out, "stderr_tail": err[-200:],
                            "limit_text": r.limit_text, "api_retry": r.api_retry})
                + "\n")
    if r.limit_text or (QUOTA_RETRIES & set(r.api_retry) and not r.ok):
        raise QuotaWait(f"{stage}: " + (r.limit_text or f"api_retry {sorted(QUOTA_RETRIES & set(r.api_retry))} "
                                                         "and no result"))
    return r
