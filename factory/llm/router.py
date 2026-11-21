"""factory/llm/router.py: Multi-provider LLM router with automatic failover."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from ..claude_call import CallResult
from .adapters import call_cli, call_gemini, call_openai_compatible
from .config import get_step_provider, load_providers_config


def _model_for(p: str, kind: str, all_cfg: dict) -> str:
    spec = all_cfg.get(p, {})
    if isinstance(spec, dict):
        if "model" in spec:
            return spec["model"]
        if "models" in spec:
            m_map = spec["models"]
            return m_map.get(kind) or m_map.get("default", "")
    return ""


def _protocol_for(p: str, all_cfg: dict) -> str:
    spec = all_cfg.get(p) if isinstance(all_cfg.get(p), dict) else {}
    proto = str(spec.get("protocol") or "").strip().lower()
    if proto:
        return proto
    if "executable" in spec:
        return "cli"
    if p == "gemini":
        return "gemini"
    if p == "claude":
        return "claude"
    if "base_url" in spec or p == "openai":
        return "openai"
    return ""


def route_call(stage: str, prompt: str, *, cwd: Path, tools: tuple[str, ...] = (),
               schema: dict | None = None, system_file: Path | None = None,
               add_dirs: tuple[Path, ...] = (), claude_bin: list[str] | None = None,
               usage_log: Path | None = None, sensor: Path | None = None,
               allow: tuple[str, ...] = (), transcript: Path | None = None,
               provider: str | None = None, model: str | None = None,
               legacy_claude_fn=None) -> CallResult:
    kind = stage.split(":")[-1]
    cfg_provider, _cfg_model, fallbacks = get_step_provider(kind)
    all_cfg = load_providers_config()

    chosen_provider = provider or cfg_provider
    candidates = [chosen_provider] + [fb for fb in fallbacks if fb != chosen_provider]
    last_err: Exception | None = None

    for p in candidates:
        target_model = (model if (p == chosen_provider and model) else None) or _model_for(p, kind, all_cfg)
        proto = _protocol_for(p, all_cfg)
        try:
            if proto == "cli":
                r = call_cli(stage, prompt, cwd=cwd, provider_name=p, model=target_model, schema=schema,
                             system_file=system_file, add_dirs=add_dirs, tools=tools,
                             transcript=transcript)
                if r.ok:
                    _log_usage(usage_log, stage, p, target_model or "default", r)
                    return r
                last_err = RuntimeError(f"{p} returned non-success: {r.text[:200]}")

            elif proto == "gemini":
                r = call_gemini(stage, prompt, cwd=cwd, provider_name=p, model=target_model, schema=schema,
                                system_file=system_file, tools=tools, transcript=transcript)
                if r.ok:
                    _log_usage(usage_log, stage, p, target_model or "gemini-3.8-flash", r)
                    return r
                last_err = RuntimeError(f"{p} returned non-success: {r.text[:200]}")

            elif proto == "claude":
                claude_fn = legacy_claude_fn
                if claude_fn is None:
                    from ..claude_call import call as claude_fn
                r = claude_fn(stage, prompt, cwd=cwd, tools=tools, schema=schema,
                              system_file=system_file, add_dirs=add_dirs,
                              claude_bin=claude_bin, usage_log=usage_log,
                              sensor=sensor, allow=allow, transcript=transcript)
                return r

            elif proto == "openai":
                r = call_openai_compatible(stage, prompt, cwd=cwd, provider_name=p,
                                           model=target_model, schema=schema, system_file=system_file,
                                           transcript=transcript)
                if r.ok:
                    _log_usage(usage_log, stage, p, target_model or "default", r)
                    return r
                last_err = RuntimeError(f"{p} returned non-success: {r.text[:200]}")

        except Exception as e:
            last_err = e
            continue

    if last_err is not None:
        raise last_err
    raise RuntimeError(f"No LLM provider succeeded for stage {stage!r} (attempted {candidates})")


def _log_usage(usage_log: Path | None, stage: str, provider: str, model: str, r: CallResult) -> None:
    if usage_log is None:
        return
    try:
        usage_log.parent.mkdir(parents=True, exist_ok=True)
        with usage_log.open("a", encoding="utf-8") as f:
            f.write(json.dumps({
                "at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "stage": stage,
                "provider": provider,
                "model": model,
                "seconds": r.seconds,
                "exit": r.exit_code,
                "ok": r.ok,
            }) + "\n")
    except Exception:
        pass
