"""The shared sound library (Plan 2d Task 1; design 6b, 8.5 item 21; FAILURE_MODES.md, sound 1-8). Public-domain sounds
found through Openverse, earshot's code-made bank and the writer's own code-made sounds. Every file is checked as
untrusted data and recorded in a tracked manifest, the licence ledger. Only licences that need no credit (operator,
5 Nov): CC0 and the Public Domain Mark. Made using Openverse, but not endorsed or certified by Openverse."""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

from .. import ROOT
from ..audio import EARSHOT_PY, SR, _ffmpeg, earshot_sfx, seconds

ALLOWED = {"cc0", "pdm"}  # no licence that needs a credit (operator, 5 Nov)
OK_LICENCES = ALLOWED | {"earshot-mit", "code-made"}  # what may sound in a video
LICENCE_URL = {"cc0": "creativecommons.org/publicdomain/zero/1.0", "pdm": "creativecommons.org/publicdomain/mark/1.0"}
MAX_BYTES, MAX_SECONDS, TIMEOUT_S = 20 * 1024 * 1024, 180.0, 30
UA = "autovideo-factory/1 (an operator's own video tool, made using Openverse)"


class SoundRefused(RuntimeError):
    def __init__(self, code: str, message: str):
        super().__init__(f"{code}: {message}")
        self.code = code


def lib_dir() -> Path:
    return Path(os.environ.get("FACTORY_SFX_LIBRARY") or ROOT / "factory" / "sfx_library")


def _api() -> str:
    return (os.environ.get("FACTORY_OPENVERSE_URL") or "https://api.openverse.org").rstrip("/")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def rows() -> list[dict]:
    m = lib_dir() / "manifest.jsonl"
    return [json.loads(x) for x in m.read_text(encoding="utf-8").splitlines() if x.strip()] if m.exists() else []


def _append(row: dict) -> dict:
    m = lib_dir() / "manifest.jsonl"
    m.parent.mkdir(parents=True, exist_ok=True)
    with m.open("a", encoding="utf-8") as f:
        f.write(json.dumps(row, sort_keys=True) + "\n")
    return row


def clean_title(text) -> str:
    return re.sub(r"[^A-Za-z0-9 ,.'-]", "", str(text or ""))[:60].strip()


def clean_tags(tags) -> list[str]:
    out = []
    for t in tags or []:
        name = t.get("name") if isinstance(t, dict) else t
        tag = re.sub(r"[^a-z0-9-]", "", re.sub(r"\s+", "-", str(name or "").lower()))[:24].strip("-")
        if tag and tag not in out:
            out.append(tag)
    return out[:8]


def _get(url: str, limit: int = 2_000_000) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=TIMEOUT_S) as r:
        return r.read(limit)


def _refusal(item: dict) -> str | None:
    if item.get("license") not in ALLOWED:
        return "licence"
    if item.get("mature"):
        return "mature"
    if item.get("category") == "music":
        return "music"
    return None


def _earshot_bank() -> list[dict]:
    cache = lib_dir() / "earshot_bank.json"
    if not cache.exists():
        try:
            r = subprocess.run([str(Path(os.environ.get("FACTORY_EARSHOT_PY", EARSHOT_PY))), "-I", "-m", "earshot", "sfx",
                                "list", "--json"], capture_output=True, text=True, encoding="utf-8", errors="replace",
                               timeout=300)
        except OSError:
            return []
        if r.returncode:
            return []
        cache.parent.mkdir(parents=True, exist_ok=True)
        cache.write_text(r.stdout, encoding="utf-8")
    try:
        return json.loads(cache.read_text(encoding="utf-8"))["sounds"]
    except (ValueError, KeyError):
        return []


def _words(text: str) -> set[str]:
    return {w for w in re.findall(r"[a-z0-9]+", text.lower()) if len(w) > 2}


def search(query: str, *, limit: int = 10) -> dict:
    """Sanitised results, up to limit from each source in turn: the library, earshot's bank, then Openverse (CC0 and
    PDM only), so one busy source never hides the next. Openverse failing gives openverse "unavailable", never raises."""
    words, results, seen, per = _words(query), [], set(), {}

    def add(r: dict) -> None:
        tier = "local" if r["local"] else r["provider"] if r["provider"] == "earshot" else "openverse"
        if r["id"] not in seen and per.get(tier, 0) < limit:
            seen.add(r["id"])
            per[tier] = per.get(tier, 0) + 1
            results.append(r)

    def hits(*texts) -> int:
        return len(words & _words(" ".join(str(t) for t in texts)))
    for _, row in sorted(((h, r) for r in rows() if (h := hits(r.get("title"), *r.get("tags", [])))), key=lambda x: -x[0]):
        add({"id": row["id"], "title": clean_title(row.get("title")), "tags": clean_tags(row.get("tags")),
             "duration_s": row.get("duration_s"), "license": row["license"], "provider": row["provider"], "local": True})
    for _, b in sorted(((h, b) for b in _earshot_bank() if (h := hits(b.get("id"), b.get("category"), b.get("notes"),
                                                                         *b.get("kinds", [])))), key=lambda x: -x[0]):
        add({"id": f"earshot:{b['id']}", "title": clean_title(b.get("notes") or b["id"]),
             "tags": clean_tags([b.get("category"), *b.get("kinds", [])]), "duration_s": b.get("dur_s"),
             "license": "earshot-mit", "provider": "earshot", "local": False})
    status = "ok"
    try:
        # ponytail: no category filter - Freesound items carry none, so category=sound_effect returned nothing (5 Nov);
        # anything Openverse does mark as music is dropped by _refusal
        q = urllib.parse.urlencode({"q": query, "license": "cc0,pdm", "mature": "false", "page_size": 20})
        for it in json.loads(_get(f"{_api()}/v1/audio/?{q}")).get("results", []):
            if _refusal(it) is None and re.fullmatch(r"[A-Za-z0-9-]{1,64}", str(it.get("id", ""))):
                add({"id": f"ov:{it['id']}", "title": clean_title(it.get("title")), "tags": clean_tags(it.get("tags")),
                     "duration_s": round((it.get("duration") or 0) / 1000, 2) or None, "license": it["license"],
                     "provider": (clean_tags([it.get("provider")]) or ["openverse"])[0], "local": False})
    except (urllib.error.URLError, OSError, ValueError):
        status = "unavailable"
    return {"results": results, "sources_tried": ["local", "earshot", "openverse"], "openverse": status}


def _download(url: str, dest: Path) -> None:
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=TIMEOUT_S) as r:
        if int(r.headers.get("Content-Length") or 0) > MAX_BYTES:
            raise SoundRefused("too_large", f"Content-Length over {MAX_BYTES} bytes")
        total = 0
        with dest.open("wb") as f:
            while chunk := r.read(65536):
                total += len(chunk)
                if total > MAX_BYTES:  # enforced while streaming: a missing or lying Content-Length can't get past
                    raise SoundRefused("too_large", f"stream over {MAX_BYTES} bytes")
                f.write(chunk)


def _probe(src: Path) -> None:
    r = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "stream=codec_type:stream_disposition=attached_pic",
                        "-of", "json", str(src)], capture_output=True, text=True, timeout=60)
    streams = json.loads(r.stdout or "{}").get("streams", []) if r.returncode == 0 else []
    audio = [s for s in streams if s.get("codec_type") == "audio"]
    if [s for s in streams if s.get("codec_type") == "video" and not (s.get("disposition") or {}).get("attached_pic")]:
        raise SoundRefused("video_stream", "the file carries a video stream")
    if len(audio) != 1:
        raise SoundRefused("not_audio", f"ffprobe found {len(audio)} audio streams")


def _normalise(src: Path, out: Path) -> None:
    """Decode only: 48 kHz stereo, leading and trailing silence trimmed, the peak at -1 dBFS."""
    tmp = out.with_suffix(".tmp.wav")
    trim = "silenceremove=start_periods=1:start_threshold=-55dB"
    _ffmpeg(["-y", "-i", str(src), "-vn", "-af", f"{trim},areverse,{trim},areverse,aresample={SR}", "-ac", "2",
             "-c:a", "pcm_s16le", str(tmp)])
    err = _ffmpeg(["-i", str(tmp), "-af", "volumedetect", "-f", "null", "-"]).stderr
    peak = float(re.search(r"max_volume:\s*(-?[\d.]+) dB", err).group(1))
    _ffmpeg(["-y", "-i", str(tmp), "-af", f"volume={-1.0 - peak:.2f}dB", "-c:a", "pcm_s16le", str(out)])
    tmp.unlink()


def get(item_id: str) -> dict:
    """The manifest row for a sound, fetching it once: an earshot bank sound renders through earshot; an Openverse item
    is re-checked on its landing page, streamed under the cap, probed and decoded. Raises SoundRefused by name."""
    for row in rows():
        if row["id"] == item_id:
            if row.get("license") not in OK_LICENCES:  # a hand-edited or old row: never into a mix
                raise SoundRefused("licence", f"{item_id} is {row.get('license')}: only public-domain, earshot and "
                                              "code-made sounds")
            return row
    kind, _, ref = item_id.partition(":")
    if kind == "earshot" and re.fullmatch(r"[a-z0-9_]+(/[a-z0-9_.]+)+", ref) and ".." not in ref:
        wav, peak = earshot_sfx(ref, lib_dir() / "earshot")
        return _append({"id": item_id, "source": "earshot", "provider": "earshot", "license": "earshot-mit",
                        "title": clean_title(ref.replace("/", " ")), "tags": clean_tags(ref.split("/")),
                        "path": wav.relative_to(lib_dir()).as_posix(), "duration_s": round(seconds(wav), 3),
                        "peak_s": peak, "sha256": _sha(wav), "read_at": _now()})
    if kind != "ov" or not re.fullmatch(r"[A-Za-z0-9-]{1,64}", ref):
        raise SoundRefused("unknown_id", f"{item_id!r} is not a library, earshot or Openverse id")
    try:
        it = json.loads(_get(f"{_api()}/v1/audio/{ref}/"))
        if code := _refusal(it):
            raise SoundRefused(code, f"{it.get('license')} {it.get('license_version') or ''}: only CC0 and the Public "
                                     "Domain Mark need no credit")
        page = _get(it["foreign_landing_url"]).decode("utf-8", "replace")
    except (urllib.error.URLError, OSError, ValueError, KeyError) as e:
        raise SoundRefused("unavailable", f"Openverse or the item's page: {e}") from None
    if LICENCE_URL[it["license"]] not in page:  # Openverse "does not verify licensing information" (6b)
        raise SoundRefused("licence_mismatch", f"the item's own page doesn't show {it['license']}")
    provider = (clean_tags([it.get("provider")]) or ["openverse"])[0]
    raw, out = lib_dir() / "raw" / ref, lib_dir() / provider / f"{ref}.wav"
    shutil.rmtree(raw, ignore_errors=True)
    raw.mkdir(parents=True)
    out.parent.mkdir(parents=True, exist_ok=True)
    try:
        src = raw / ("source." + (re.sub(r"[^a-z0-9]", "", str(it.get("filetype") or "bin").lower())[:5] or "bin"))
        _download(it["url"], src)
        _probe(src)
        try:
            _normalise(src, out)
        except (RuntimeError, AttributeError) as e:
            raise SoundRefused("not_audio", f"ffmpeg couldn't decode it: {str(e)[-160:]}") from None
        if (dur := seconds(out)) > MAX_SECONDS:
            raise SoundRefused("too_long", f"{dur:.0f} s is over {MAX_SECONDS:.0f} s")
        source_sha = _sha(src)
    except BaseException:
        out.unlink(missing_ok=True)
        raise
    finally:
        shutil.rmtree(raw, ignore_errors=True)  # the untrusted original never stays
    return _append({"id": item_id, "source": "openverse", "provider": provider, "url": it["url"],
                    "landing_url": it["foreign_landing_url"], "creator": str(it.get("creator") or "")[:120],
                    "license": it["license"], "license_version": it.get("license_version"),
                    "title": clean_title(it.get("title")), "tags": clean_tags(it.get("tags")),
                    "path": out.relative_to(lib_dir()).as_posix(), "duration_s": round(dur, 3), "peak_s": None,
                    "sha256": _sha(out), "source_sha256": source_sha, "read_at": _now()})


def path(sound_id: str) -> Path:
    """The wav the mix uses; a sound missing from this machine is fetched again by id."""
    return lib_dir() / get(sound_id)["path"]
