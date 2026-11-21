"""factory/llm/adapters/cli.py: Generic adapter for local CLI LLM runners configured with protocol="cli"."""
from __future__ import annotations

import json
import os
import re
import shutil
import time
import uuid
from pathlib import Path

from ...claude_call import CallResult
from ...procs import run_cli
from ..config import load_providers_config


def call_cli(stage: str, prompt: str, *, cwd: Path, provider_name: str,
             model: str = "", effort: str = "medium",
             schema: dict | None = None, system_file: Path | None = None,
             add_dirs: tuple[Path, ...] = (), tools: tuple[str, ...] = (),
             transcript: Path | None = None) -> CallResult:
    start_t = time.time()
    cfg = load_providers_config().get(provider_name, {})
    if not isinstance(cfg, dict):
        cfg = {}
    exe_str = cfg.get("executable") or shutil.which(provider_name) or ""
    if not exe_str or not Path(exe_str).is_file():
        raise RuntimeError(
            f"CLI executable for provider {provider_name!r} not found (configure 'executable' in secrets/providers.json)"
        )

    target_model = model or cfg.get("model", "")
    full_prompt = prompt
    if system_file and Path(system_file).is_file():
        system_text = Path(system_file).read_text(encoding="utf-8")
        full_prompt = f"### SYSTEM INSTRUCTIONS ###\n{system_text}\n\n### TASK ###\n{prompt}"

    if stage.endswith("review") or tools == ("Read",):
        full_prompt += (
            "\n\n### CRITICAL SPEED DIRECTIVE ###\n"
            "Inspect the provided evidence images directly and return your final evaluation immediately "
            "in the requested JSON schema. Do not write or execute python image-cropping or diffing scripts."
        )

    # On Windows, command line length must stay under 32k. If long, write to a unique file in cwd.
    task_file: Path | None = None
    if len(full_prompt) > 16000:
        task_file = cwd / f"_prompt_task_{uuid.uuid4().hex[:8]}.md"
        task_file.write_text(full_prompt, encoding="utf-8")
        cli_prompt = (
            f"Read and follow all instructions in ./{task_file.name} located in your current directory. "
            "Complete the task exactly as requested."
        )
    else:
        cli_prompt = full_prompt

    cmd = [exe_str, "-p", cli_prompt]
    if target_model:
        cmd += ["--model", target_model]
    cmd += ["--dangerously-skip-permissions"]
    if effort and not any(target_model.endswith(f"-{lvl}") for lvl in ("low", "medium", "high", "xhigh", "max")):
        cmd += ["--effort", effort]
    if schema:
        cmd += ["--output-format", "json", "--json-schema", json.dumps(schema)]
    else:
        cmd += ["--output-format", "text"]

    if any(t in tools for t in ("Write", "Edit")):
        cmd += ["--mode", "accept-edits"]

    for d in add_dirs:
        cmd += ["--add-dir", str(d)]

    try:
        proc = run_cli(cmd, cwd=cwd, env=os.environ.copy(), timeout_s=900)
    finally:
        if task_file is not None:
            task_file.unlink(missing_ok=True)
    elapsed = time.time() - start_t
    out, err = proc.stdout or "", proc.stderr or ""
    if transcript is not None:
        transcript.write_text(out + "\n" + err, encoding="utf-8")

    r = CallResult(stage, proc.returncode, proc.timed_out, elapsed)
    if proc.returncode != 0:
        r.ok = False
        r.text = err or out
        return r

    if schema:
        try:
            parsed = json.loads(out)
            raw_struct = parsed.get("structured_output")
            if not raw_struct and parsed.get("response"):
                resp_text = parsed["response"].strip()
                if resp_text.startswith("```"):
                    resp_text = re.sub(r"^```(?:json)?\s*", "", resp_text, flags=re.IGNORECASE)
                    resp_text = re.sub(r"\s*```$", "", resp_text)
                try:
                    raw_struct = json.loads(resp_text)
                except Exception:
                    pass
            if isinstance(raw_struct, dict):
                clean = {}
                for k, v in raw_struct.items():
                    if isinstance(v, str) and (v.strip().startswith("{") or v.strip().startswith("[")):
                        try:
                            v = json.loads(v)
                            if isinstance(v, dict) and k in v:
                                v = v[k]
                        except Exception:
                            pass
                    clean[k] = v
                r.data = clean
                r.ok = True
            else:
                r.data = raw_struct
                r.ok = raw_struct is not None
            r.text = parsed.get("response", "")
            r.turns = parsed.get("num_turns", 1)
        except Exception:
            m = re.search(r"\{.*\}", out, re.DOTALL)
            if m:
                try:
                    r.data = json.loads(m.group(0))
                    r.ok = True
                except Exception:
                    r.ok = False
            else:
                r.ok = False
            r.text = out
    else:
        r.ok = True
        r.text = out

    return r
