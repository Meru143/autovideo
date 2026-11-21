"""factory/llm/config.py: Provider credentials and endpoint configuration."""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from .. import ROOT, settings


def _read_env_file(path: Path) -> dict[str, str]:
    if not path.exists():
        return {}
    out = {}
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        out[k.strip()] = v.strip().strip("\"'")
    return out


def resolve_api_key(key_name: str) -> str:
    """Finds an API key from system environment or any file in secrets/."""
    if val := os.environ.get(key_name):
        return val.strip()
    secrets_dir = ROOT / "secrets"
    if secrets_dir.is_dir():
        for env_file in secrets_dir.glob("*.env"):
            env_map = _read_env_file(env_file)
            if key_name in env_map:
                return env_map[key_name]
    return ""


def load_providers_config() -> dict[str, Any]:
    """Loads secrets/providers.json if available, with defaults."""
    cfg_file = ROOT / "secrets" / "providers.json"
    if cfg_file.exists():
        try:
            return json.loads(cfg_file.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {}


def get_step_provider(stage_kind: str) -> tuple[str, str, list[str]]:
    """Returns (provider_name, model_name, fallback_list) for a given stage."""
    p_cfg = load_providers_config()
    default_provider = os.environ.get("FACTORY_PROVIDER") or p_cfg.get("active_provider") or "claude"
    step_provider = p_cfg.get("steps", {}).get(stage_kind, default_provider)

    provider_spec = p_cfg.get(step_provider, {})
    model = provider_spec.get("model", "") if isinstance(provider_spec, dict) else ""
    if not model and isinstance(provider_spec, dict) and "models" in provider_spec:
        models_map = provider_spec["models"]
        model = models_map.get(stage_kind) or models_map.get("default", "")

    fallbacks = p_cfg.get("fallback_providers", ["gemini", "claude"])
    return step_provider, model, fallbacks
