"""factory/llm/adapters/gemini.py: Native Google Gemini REST API adapter (zero external SDK deps)."""
from __future__ import annotations

import json
import re
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from ...claude_call import CallResult
from ..config import load_providers_config, resolve_api_key

API_BASE = "https://generativelanguage.googleapis.com/v1beta/models"


def _normalize_model(model: str) -> tuple[str, str | None]:
    """Strip optional -low/-medium/-high effort suffix if present and return (model_id, thinking_level)."""
    m = (model or "gemini-3.8-flash").strip()
    for lvl in ("low", "medium", "high"):
        if m.endswith(f"-{lvl}"):
            return m[: -(len(lvl) + 1)], lvl
    return m, None


def call_gemini(stage: str, prompt: str, *, cwd: Path, provider_name: str = "gemini", model: str = "",
                schema: dict | None = None, system_file: Path | None = None,
                tools: tuple[str, ...] = (), transcript: Path | None = None) -> CallResult:
    start_t = time.time()
    cfg = load_providers_config().get(provider_name, {})
    if not isinstance(cfg, dict):
        cfg = {}
    key_name = cfg.get("env_key", "GEMINI_API_KEY")
    api_key = resolve_api_key(key_name) or resolve_api_key("GEMINI_API_KEY")
    if not api_key:
        raise RuntimeError(f"Gemini API key not found for {key_name} in secrets/ or environment")

    target_model, _ = _normalize_model(model or cfg.get("model", "gemini-3.8-flash"))
    payload: dict[str, Any] = {
        "contents": [{"role": "user", "parts": [{"text": prompt}]}],
    }
    if system_file and Path(system_file).is_file():
        payload["systemInstruction"] = {"parts": [{"text": Path(system_file).read_text(encoding="utf-8")}]}
    if schema:
        payload["generationConfig"] = {
            "responseMimeType": "application/json",
            "responseSchema": schema,
        }

    url = f"{API_BASE}/{target_model}:generateContent"
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json", "x-goog-api-key": api_key},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=180) as resp:
            body = json.loads(resp.read().decode("utf-8"))
        parts = (((body.get("candidates") or [{}])[0].get("content") or {}).get("parts") or [])
        text = "".join(str(pt.get("text") or "") for pt in parts).strip()
    except urllib.error.HTTPError as e:
        elapsed = time.time() - start_t
        detail = e.read().decode("utf-8", errors="replace")[:400]
        r = CallResult(stage, 1, False, elapsed)
        r.ok = False
        r.text = f"Gemini HTTP {e.code}: {detail}"
        return r
    except Exception as e:
        elapsed = time.time() - start_t
        r = CallResult(stage, 1, False, elapsed)
        r.ok = False
        r.text = str(e).replace(api_key, "<key>")
        return r

    elapsed = time.time() - start_t
    if transcript is not None:
        transcript.write_text(text, encoding="utf-8")

    r = CallResult(stage, 0, False, elapsed)
    r.text = text
    if schema:
        try:
            r.data = json.loads(text)
            r.ok = True
        except Exception:
            m = re.search(r"\{.*\}", text, re.DOTALL)
            if m:
                try:
                    r.data = json.loads(m.group(0))
                    r.ok = True
                except Exception:
                    r.ok = False
            else:
                r.ok = False
    else:
        r.ok = bool(text)

    return r
