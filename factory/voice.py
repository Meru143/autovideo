"""Voice stage (design 5.4.5; 8.5 items 1-2; FAILURE_MODES.md, voice). A channel speaks through Gemini 3.8 Flash
TTS (a prebuilt voice on the free tier, one request per video) or through the local clone, as its workspace says.
Every narration is transcribed offline and checked: word error rate, opening words and speaking rate.

How 3.8 takes direction: the text is spoken verbatim, so style goes only in a speech_metadata annotation; pauses
are punctuation or <short pause> tags. Keys come from secrets/gemini.env and are never printed."""
from __future__ import annotations

import base64
import io
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
import wave
from pathlib import Path

from . import OUT, ROOT
from .gpu import gpu_lock

API = "https://generativelanguage.googleapis.com/v1beta/interactions"
MODEL = "gemini-3.8-flash-tts"
KEY_NAME = "GEMINI_API_KEY"
DAILY, SPACING_S = 10, 21.0  # free tier: 10 requests a day and 3 a minute per project
MIN_AUDIO_BYTES = 4800  # 0.1 s of 24 kHz s16: less is no narration
MAX_PITCH_RESIDUAL_ST = 0.25  # the clone's pitch median must land this close to the real narrator's


def voice_config(brand: str) -> dict:
    """The workspace's voice section (engine, voice, style, rate band): data the operator edits, not code."""
    from .workspace import load
    return load(brand, need=("voice",))["voice"]


class QuotaExhausted(RuntimeError):
    pass


class VoiceCheckFailed(RuntimeError):
    def __init__(self, findings: list[str]):
        super().__init__("; ".join(findings))
        self.findings = findings


def _key() -> str:
    sec_dir = Path(os.environ.get("FACTORY_SECRETS", ROOT / "secrets"))
    env = sec_dir / "gemini.env"
    candidates: list[str] = []
    if forced := os.environ.get("FACTORY_GEMINI_KEY_NAME", "").strip():
        candidates.append(forced)
    prov_file = sec_dir / "providers.json"
    if prov_file.is_file():
        try:
            g_cfg = json.loads(prov_file.read_text(encoding="utf-8")).get("gemini")
            if isinstance(g_cfg, dict) and (ek := str(g_cfg.get("env_key") or "").strip()):
                candidates.append(ek)
        except Exception:
            pass
    if KEY_NAME not in candidates:
        candidates.append(KEY_NAME)
    lines = env.read_text(encoding="utf-8-sig").splitlines() if env.exists() else []
    for name in candidates:
        for line in lines:
            if line.strip().startswith(f"{name}="):
                if key := line.split("=", 1)[1].strip().strip("\"'"):
                    return key
                raise RuntimeError(f"{name} is empty in {env}")
        if val := os.environ.get(name, "").strip():
            return val
    raise RuntimeError(f"{candidates[0]} is missing from {env}")


def split_c2pa(pcm: bytes) -> tuple[bytes, bytes]:
    """(audio, manifest): Google appends a C2PA (JUMBF) manifest to the samples, which plays as a full-scale burst. The
    first "jumb" box header with "c2pa" within 64 bytes starts it, wherever it is (a manifest can pass 64 KB); a chance
    "jumb" in the audio without "c2pa" beside it stays. The manifest is kept as a provenance record (Plan 2b, 7)."""
    i = pcm.find(b"jumb")
    while i != -1:
        if i >= 4 and b"c2pa" in pcm[i:i + 64]:
            cut = i - 4  # the box starts with its 4-byte size
            cut -= cut % 2
            return pcm[:cut], pcm[cut:]
        i = pcm.find(b"jumb", i + 1)
    return pcm, b""


def strip_c2pa(pcm: bytes) -> bytes:
    return split_c2pa(pcm)[0]


def _budget(ledger: Path) -> None:
    rows = [json.loads(x) for x in ledger.read_text(encoding="utf-8").splitlines() if x.strip()] if ledger.exists() else []
    now = time.time()
    # ponytail: a rolling 24 h window is stricter than Google's midnight-Pacific reset; switch if it blocks needed runs
    if sum(1 for r in rows if r["status"] == 200 and now - r["at"] < 86400) >= DAILY:
        raise QuotaExhausted(f"{DAILY} Gemini TTS requests in the last 24 h (free tier)")
    time.sleep(max(0.0, max((r["at"] for r in rows), default=0.0) + SPACING_S - now))


def _record(ledger: Path, brand: str, status: int | str) -> None:
    ledger.parent.mkdir(parents=True, exist_ok=True)
    with ledger.open("a", encoding="utf-8") as f:
        f.write(json.dumps({"at": time.time(), "brand": brand, "status": status}) + "\n")


def speak(text: str, voice: str, style: str, *, brand: str = "", ledger: Path | None = None,
          manifest_out: Path | None = None) -> bytes:
    """One Interactions request (a retry only for a per-minute 429, a 5xx or the network); 24 kHz mono s16 PCM with
    the C2PA manifest cut off, and saved to manifest_out when given."""
    key, ledger = _key(), ledger or OUT / "gemini_requests.jsonl"
    body = json.dumps({"model": MODEL, "response_format": {"type": "audio"},
                       "generation_config": {"speech_config": [{"voice": voice}]},
                       "input": [{"type": "user_input", "content": [{"type": "text", "text": text, "annotations": [
                           {"type": "speech_metadata", "style": style}]}]}]}).encode()
    minute_retried, retries = False, 0
    while True:
        _budget(ledger)
        req = urllib.request.Request(os.environ.get("FACTORY_GEMINI_URL", API), data=body,
                                     headers={"x-goog-api-key": key, "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=300) as r:
                reply = json.loads(r.read())
            _record(ledger, brand, 200)
            break
        except urllib.error.HTTPError as e:
            said = e.read().decode("utf-8", "replace").replace(key, "<key>")
            _record(ledger, brand, e.code)
            if e.code == 429 and "minute" in said.lower() and not minute_retried:
                minute_retried = True  # _budget waits out the spacing before the retry
                continue
            if e.code == 429:
                raise QuotaExhausted(f"Gemini 429: {said[:300]}") from None
            if e.code < 500 or retries == 2:
                raise RuntimeError(f"Gemini HTTP {e.code}: {said[:300]}") from None
        except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
            _record(ledger, brand, "network")
            if retries == 2:
                raise RuntimeError(f"Gemini unreachable: {str(e).replace(key, '<key>')[:200]}") from None
        time.sleep(30 * 2 ** retries)  # 5xx or network: 30 s, then 60 s (5.5.5: its own retry budget)
        retries += 1
    parts = [c for s in reply.get("steps", []) if s.get("type") == "model_output" for c in s.get("content", [])]
    audio = [c for c in parts if c.get("type") == "audio"]
    if not audio:
        said = " ".join(c.get("text", "") for c in parts)[:150]
        raise RuntimeError(f"Gemini returned no audio ({[c.get('type') for c in parts]}; {said!r})")
    data = base64.b64decode(audio[-1]["data"])
    if data[:4] == b"RIFF":
        with wave.open(io.BytesIO(data)) as w:
            if (w.getnchannels(), w.getsampwidth(), w.getframerate()) != (1, 2, 24000):
                raise RuntimeError(f"unexpected audio format {w.getparams()}")
            data = w.readframes(w.getnframes())
    pcm, manifest = split_c2pa(data)
    if len(pcm) < MIN_AUDIO_BYTES:
        raise RuntimeError(f"Gemini returned no usable audio ({len(pcm)} bytes of samples)")
    if manifest_out and manifest:
        manifest_out.write_bytes(manifest)
    return pcm


_NUMBERS = None


def _words(text: str) -> list[str]:
    """Words as heard: tags dropped, spoken numbers as digits (Whisper's own normaliser: "forty" -> 40, "ninety
    percent" -> 90%), symbols spelled, "okay" as "ok", hyphens split, edge quotes stripped. Both sides of a comparison
    go through it."""
    global _NUMBERS
    if _NUMBERS is None:
        from transformers.models.whisper.english_normalizer import EnglishNumberNormalizer
        _NUMBERS = EnglishNumberNormalizer()
    t = _NUMBERS(re.sub(r"<[^>]*>", " ", text.lower().replace("’", "'")))
    t = re.sub(r"\ba (?=\d)", "", t).replace("%", " percent").replace("&", " and ")  # "a 100" is said as "a hundred"
    t = re.sub(r"\bokay\b", "ok", t)  # one word, two spellings (9 Nov: a clean read failed on three of them)
    return [w.strip("'") for w in re.findall(r"[a-z0-9']+", re.sub(r"(?<=\w)-(?=\w)", " ", t)) if w.strip("'")]


def _join_splits(a: list[str], b: list[str]) -> list[str]:
    """Join adjacent words in a that b writes as one ("every one" / "everyone"): they sound the same."""
    whole, out, i = set(b), [], 0
    while i < len(a):
        if i + 1 < len(a) and a[i] + a[i + 1] in whole:
            out.append(a[i] + a[i + 1])
            i += 2
        else:
            out.append(a[i])
            i += 1
    return out


_SOUNDEX = {**dict.fromkeys("bfpv", "1"), **dict.fromkeys("cgjkqsxz", "2"), **dict.fromkeys("dt", "3"), "l": "4",
            **dict.fromkeys("mn", "5"), "r": "6"}


def _soundex(w: str) -> str:
    out, last = w[0], _SOUNDEX.get(w[0], "")
    for ch in w[1:]:
        code = _SOUNDEX.get(ch, "")
        if code and code != last:
            out += code
        if ch not in "hw":
            last = code
    return (out + "000")[:4]


def _alike(r: str, h: str) -> bool:
    """Spelled apart, heard the same ("grey"/"gray", "route"/"root", "boss's"/"bosses", "2"/"two"): the check is about
    what a listener hears. An apostrophe counts as a vowel, which it is where it splits two like sounds ("boss's")."""
    if r.isdigit() != h.isdigit():  # the normaliser spells a number out on one side only ("one, two, or three")
        return _NUMBERS(r) == _NUMBERS(h)
    r, h = r.replace("'", "e"), h.replace("'", "e")
    return r == h or (len(r) >= 3 and len(h) >= 3 and r.isalpha() and h.isalpha() and _soundex(r) == _soundex(h))


def _wer(ref: list[str], hyp: list[str]) -> float:
    prev = list(range(len(hyp) + 1))
    for i, r in enumerate(ref, 1):
        cur = [i]
        for j, h in enumerate(hyp, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (not _alike(r, h))))
        prev = cur
    return prev[-1] / max(len(ref), 1)


def transcribe(wav: Path, out_dir: Path) -> dict:
    """Local WhisperX (tools/transcribe.py, offline guard on): {text, start, end} with the first and last word times."""
    with gpu_lock():  # one heavy GPU job at a time, across the videos running side by side (9 Nov)
        r = subprocess.run([sys.executable, str(ROOT / "tools" / "transcribe.py"), str(wav), "--out-dir", str(out_dir),
                            "--model", "small", "--language", "en"], cwd=ROOT, capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=1800)
    if r.returncode != 0:
        raise RuntimeError(f"transcribe.py exited {r.returncode}: {r.stderr[-400:]}")
    t = json.loads((out_dir / "transcript.json").read_text(encoding="utf-8"))
    if t.get("network_attempts_during_transcription"):
        raise RuntimeError(f"WhisperX tried the network: {t['network_attempts_during_transcription']}")
    return _asr_summary(t)


def _asr_summary(t: dict) -> dict:
    """{text, start, end}: the words WhisperX timed (it stores None for a word it couldn't align, such as digits)."""
    words = [w for s in t.get("segments", []) for w in s.get("words", [])
             if w.get("start") is not None and w.get("end") is not None]
    return {"text": " ".join(s.get("text", "").strip() for s in t.get("segments", [])),
            "start": words[0]["start"] if words else 0.0, "end": words[-1]["end"] if words else 0.0}


def voice_checks(wav: Path, script: str, brand: str) -> dict:
    return check_narration(transcribe(wav, wav.parent / "_asr"), wav, script, voice_config(brand))


def check_narration(asr: dict, wav: Path, script: str, cfg: dict) -> dict:
    """The voice checks on a transcript: word error rate, opening words, speaking rate in the workspace's band."""
    said, spoken = asr["text"], len(_words(script))
    ref, hyp = _words(script), _words(said)
    ref = _join_splits(ref, hyp)
    hyp = _join_splits(hyp, ref)
    with wave.open(str(wav)) as w:
        seconds = w.getnframes() / w.getframerate()
    unit, lo, hi = cfg["rate"]["unit"], cfg["rate"]["lo"], cfg["rate"]["hi"]
    speech = asr["end"] - asr["start"] if asr["end"] > asr["start"] else seconds  # head and tail silence excluded
    rate = spoken / speech * (60 if unit == "wpm" else 1)
    wer = _wer(ref, hyp)
    findings = [f"word error rate {wer:.1%} is over 3%"] * (wer > 0.03)
    if hyp[:2] != ref[:2]:
        findings.append(f"opens with {' '.join(hyp[:4])!r}, not the script's {' '.join(ref[:4])!r}")
    if not lo <= rate <= hi:
        findings.append(f"rate {rate:.1f} {unit} is outside {lo}-{hi}")
    return {"wer": round(wer, 4), "opening_ok": hyp[:2] == ref[:2], "heard_opening": " ".join(hyp[:6]),
            "seconds": round(seconds, 2), "speech_s": round(speech, 2), "rate": round(rate, 2), "rate_unit": unit, "heard": said,
            "findings": findings}


def _clone(script: str, out_wav: Path, cfg: dict, beats: list[dict] | None = None, attempt: int = 1) -> dict:
    """A cloned voice: factory/clone_voice.py inside voice-clone-env (GPU; CPU if it runs out of memory), with the
    channel's reference clip, real vocals, tuning target and phrasing from its voice settings. Story beats give the
    pauses and intensity by role; plain text reads as one beat (FAILURE_MODES.md, clone phrasing)."""
    py = Path(os.environ.get("FACTORY_CLONE_PY", Path.home() / "Downloads" / "voice-clone-env" / "Scripts" / "python.exe"))
    if not py.exists():
        raise RuntimeError(f"voice-clone-env's python is not at {py} (set FACTORY_CLONE_PY)")
    spec = out_wav.with_name("clone_input.json")
    spec.write_text(json.dumps({"beats": beats or [{"text": script, "role": None}], "phrasing": cfg["phrasing"],
                                "seed_offset": 1_000_000 * (attempt - 1)}, indent=2), encoding="utf-8")
    with gpu_lock():  # the cloned voice runs on the GPU: one heavy GPU job at a time (9 Nov)
        r = subprocess.run([str(py), str(ROOT / "factory" / "clone_voice.py"), "--input", str(spec), "--out",
                            str(out_wav), "--prompt", str(ROOT / cfg["prompt"]),
                            "--real-vocals", str(ROOT / cfg["real_vocals"]), "--tune", str(ROOT / cfg["tune"])], cwd=ROOT,
                           capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=3600)
    if r.returncode != 0:
        raise RuntimeError(f"clone_voice.py exited {r.returncode}: {r.stderr[-600:]}")
    return json.loads(r.stdout.strip().splitlines()[-1])


def clone_findings(info: dict) -> list[str]:
    """The clone's own checks: its pitch median lands on the real narrator's, and every take passed its take check
    (clone_voice.py reports both)."""
    residual, out = info.get("pitch_residual_st"), []
    if residual is None or abs(residual) >= MAX_PITCH_RESIDUAL_ST:
        out.append(f"clone pitch residual {residual} st is not under {MAX_PITCH_RESIDUAL_ST}")
    if failed := info.get("takes_failed"):
        out.append(f"{len(failed)} take(s) whispered, cut short or rambling after 3 tries: {failed}")
    return out


def synthesize(brand: str, script: str, out_dir: Path, *, ledger: Path | None = None,
               beats: list[dict] | None = None, attempt: int = 1) -> dict:
    """Write out_dir/narration.wav (24 kHz mono, -16 LUFS / -2 dBTP), narration.c2pa when Gemini sends a manifest,
    and voice.json; raise VoiceCheckFailed. A cloned voice reads story beats ({text, role}) when given them."""
    cfg = voice_config(brand)
    out_dir.mkdir(parents=True, exist_ok=True)
    raw, extra = out_dir / "narration.raw.wav", {}
    if cfg["engine"] == "clone":
        extra = {"clone": _clone(script, raw, cfg, beats, attempt)}  # a retry reads with fresh seeds
    else:
        pcm = speak(script, cfg["voice"], cfg["style"], brand=brand, ledger=ledger,
                    manifest_out=out_dir / "narration.c2pa")
        with wave.open(str(raw), "wb") as w:
            w.setnchannels(1), w.setsampwidth(2), w.setframerate(24000), w.writeframes(pcm)
    wav = out_dir / "narration.wav"
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", str(raw), "-af", "loudnorm=I=-16:TP=-2:LRA=11", "-ar", "24000",
                    "-ac", "1", str(wav)], check=True, timeout=600)
    raw.unlink()
    result = {"brand": brand, "engine": cfg["engine"], "voice": cfg.get("voice"), "style": cfg.get("style"), **extra,
              **voice_checks(wav, script, brand)}
    if "clone" in extra:
        result["findings"] += clone_findings(extra["clone"])
    (out_dir / "voice.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
    if result["findings"]:
        raise VoiceCheckFailed(result["findings"])
    return result
