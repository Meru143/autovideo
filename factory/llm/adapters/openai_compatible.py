"""factory/llm/adapters/openai_compatible.py: Universal adapter for OpenAI-compatible endpoints
(OpenAI, DeepSeek, OpenRouter, Ollama, vLLM, or any custom provider in secrets/providers.json)."""
from __future__ import annotations

import json
import re
import time
from pathlib import Path
from typing import Any

from ...claude_call import CallResult
from ..config import load_providers_config, resolve_api_key


def call_openai_compatible(stage: str, prompt: str, *, cwd: Path, provider_name: str,
                           model: str = "", schema: dict | None = None,
                           system_file: Path | None = None,
                           transcript: Path | None = None) -> CallResult:
    start_t = time.time()
    all_cfg = load_providers_config()
    cfg = all_cfg.get(provider_name, {})
    base_url = cfg.get("base_url") or ("https://api.openai.com/v1" if provider_name == "openai" else None)
    if not base_url:
        raise RuntimeError(f"No base_url configured for provider {provider_name!r} in secrets/providers.json")

    key_env = cfg.get("env_key", f"{provider_name.upper()}_API_KEY")
    api_key = resolve_api_key(key_env)
    if not api_key:
        raise RuntimeError(f"API key {key_env!r} not found for provider {provider_name!r}")

    from openai import OpenAI
    client = OpenAI(base_url=base_url, api_key=api_key, timeout=180.0)

    target_model = model or (cfg.get("models", {}).get("default") if isinstance(cfg.get("models"), dict) else None) or cfg.get("model")
    if not target_model:
        raise RuntimeError(f"No model specified for provider {provider_name!r}")

    messages: list[dict[str, str]] = []
    if system_file and Path(system_file).is_file():
        system_text = Path(system_file).read_text(encoding="utf-8")
        messages.append({"role": "system", "content": system_text})

    if schema:
        prompt_with_schema = f"{prompt}\n\nRespond with strict JSON matching this schema:\n{json.dumps(schema, indent=2)}"
        messages.append({"role": "user", "content": prompt_with_schema})
    else:
        messages.append({"role": "user", "content": prompt})

    kwargs: dict[str, Any] = {"model": target_model, "messages": messages}
    if schema:
        try:
            # Try native json_schema format
            kwargs["response_format"] = {"type": "json_schema", "json_schema": {"name": stage, "schema": schema}}
        except Exception:
            kwargs["response_format"] = {"type": "json_object"}

    try:
        resp = client.chat.completions.create(**kwargs)
        text = resp.choices[0].message.content or ""
    except Exception as e:
        # Retry with json_object format if strict schema was rejected
        if "response_format" in kwargs and kwargs["response_format"].get("type") == "json_schema":
            try:
                kwargs["response_format"] = {"type": "json_object"}
                resp = client.chat.completions.create(**kwargs)
                text = resp.choices[0].message.content or ""
            except Exception as e2:
                elapsed = time.time() - start_t
                r = CallResult(stage, 1, False, elapsed)
                r.ok = False
                r.text = str(e2)
                return r
        else:
            elapsed = time.time() - start_t
            r = CallResult(stage, 1, False, elapsed)
            r.ok = False
            r.text = str(e)
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
        r.ok = True

    return r
