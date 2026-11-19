"""Music, SFX and the master (design 6b, 8.5 items 7 and 12; Plan 2b Task 8, Plan 2c Task 5): the music writer's bed
set 12 LU under the voice and swelling in the gaps, one earshot bank sound on each declared visual event (data-event
+ data-at; never one per sentence), the narration padded to the rendered film (finding F5), and a two-pass loudnorm
master at -14 LUFS."""
from __future__ import annotations

import json
import re
import subprocess
import tempfile
from html.parser import HTMLParser
from pathlib import Path

SR = 48000
MASTER_TP = -1.5  # 0.5 dB under the -1.0 dBTP gate line, so a measured overshoot after AAC still passes
MAX_HOLD_S = 3.0  # a film may hold its closing line this long past the last word (F5)
VOICE_CHAIN = "highpass=f=75"  # the mixer's own filters, shared with its speech-over-bed measurement
BED_FILTERS = "highpass=f=110,equalizer=f=2000:t=o:w=2:g=-4"  # 6b: high-passed, dipped where speech lives
SPEECH_OVER_BED_LU, SWELL_DB, SWELL_RAMP_S, SWELL_GAP_S = 12.0, 4.0, 0.15, 0.6  # 6b: 10 LU or more; gap swells
BED_HEADROOM_LU = 6.0  # the bed's loudest 400 ms, swells included, stays this far under the voice (Plan 2c review I2)
SENTENCE_FLOOR_LU, MIN_SENTENCE_S = 10.5, 0.4  # each sentence's own gap: the render QA's 10 LU plus a margin
FOLD = "pan=stereo|c0=0.5*c0+0.5*c1|c1=0.5*c0+0.5*c1"  # a phone speaker's mono


def _ffmpeg(args: list[str], timeout_s: int = 900) -> subprocess.CompletedProcess:
    r = subprocess.run(["ffmpeg", "-hide_banner", "-nostdin", *args], capture_output=True, text=True,
                       encoding="utf-8", errors="replace", timeout=timeout_s)
    if r.returncode != 0:
        raise RuntimeError(f"ffmpeg failed: {r.stderr[-600:]}")
    return r


def seconds(path: Path) -> float:
    r = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(path)],
                       capture_output=True, text=True, timeout=60)
    return float(r.stdout.strip())


class _Events(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.events: list[tuple[str, float]] = []

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if a.get("data-event") and a.get("data-at"):
            try:
                self.events.append((a["data-event"].strip().lower(), float(a["data-at"])))
            except ValueError:
                pass


def declared_events(index_html: Path) -> list[tuple[str, float]]:
    """(kind, seconds) for every element that declares data-event and data-at, in time order."""
    p = _Events()
    p.feed(index_html.read_text(encoding="utf-8"))
    return sorted(p.events, key=lambda e: e[1])


def root_duration(index_html: Path) -> float:
    """The composition's length: data-duration on its root, data-composition-id="main"."""
    html = index_html.read_text(encoding="utf-8")
    m = (re.search(r'data-composition-id="main"[^>]*?data-duration="([\d.]+)"', html)
         or re.search(r'data-duration="([\d.]+)"[^>]*?data-composition-id="main"', html))
    if not m:
        raise ValueError('no data-duration on the root data-composition-id="main"')
    return float(m.group(1))


EARSHOT_PY = Path.home() / "Downloads" / "earshot-env" / "Scripts" / "python.exe"  # its own env (torch-free install)
# Where a sound's loudest moment lands against its event (6b): taps and knocks lead the cut, a whoosh peaks just after.
APEX_OFFSET_S = {"whoosh": 0.037, "cut": -0.05, "impact": -0.05, "counter": -0.05, "settle": 0.0}


def earshot_sfx(sound_id: str, cache: Path) -> tuple[Path, float]:
    """One take of a bank sound from earshot (MIT; made in code, audited), cached; returns (wav, peak seconds)."""
    import os
    py = Path(os.environ.get("FACTORY_EARSHOT_PY", EARSHOT_PY))
    if not py.exists():
        raise RuntimeError(f"earshot's python is not at {py} (set FACTORY_EARSHOT_PY)")
    cache.mkdir(parents=True, exist_ok=True)
    wav, meta = cache / (sound_id.replace("/", "__") + ".wav"), cache / (sound_id.replace("/", "__") + ".json")
    if not (wav.exists() and meta.exists()):
        r = subprocess.run([str(py), "-I", "-m", "earshot", "sfx", "render", sound_id, "-o", str(wav), "--json",
                            "--sr", str(SR), "--out", str(meta)], capture_output=True, text=True, encoding="utf-8",
                           errors="replace", timeout=300)
        if r.returncode != 0 or not meta.exists():
            raise RuntimeError(f"earshot sfx render {sound_id} failed: {r.stderr[-300:]}")
    info = json.loads(meta.read_text(encoding="utf-8"))["meta"]
    if not info["checks"]["ok"]:
        raise RuntimeError(f"earshot's audit failed {sound_id}: {info['checks']['summary']}")
    return wav, float(info["peak_s"])


def fit_narration(narration: Path, film_s: float, out_wav: Path, *, max_hold_s: float = MAX_HOLD_S) -> float:
    """Pad the narration with silence to the film's length (F5); returns the hold past the last word. The channel's
    format sets max_hold_s."""
    hold = film_s - seconds(narration)
    if not -0.1 <= hold <= max_hold_s:
        raise RuntimeError(f"the film runs {hold:.2f} s past the narration; allowed 0 to {max_hold_s} s")
    _ffmpeg(["-y", "-i", str(narration), "-af", f"aresample={SR},apad=whole_dur={film_s:.3f}", "-ac", "1", str(out_wav)])
    return round(hold, 3)


def _swell_gaps(spans: list[tuple[float, float]], total_s: float) -> list[tuple[float, float]]:
    edges = [(0.0, spans[0][0]), *((a[1], b[0]) for a, b in zip(spans, spans[1:])), (spans[-1][1], total_s)]
    return [(a, b) for a, b in edges if b - a >= SWELL_GAP_S]


def _chain(gain: float, spans: list[tuple[float, float]], total_s: float, swell_db: float,
           ducks: list[tuple[float, float, float]] = ()) -> str:
    ramp, boost, rise, duck = SWELL_RAMP_S, 10 ** (swell_db / 20) - 1, "0", "1"
    for a, b in _swell_gaps(spans, total_s):
        rise = f"max({rise},clip(min((t-{a:.3f})/{ramp},({b:.3f}-t)/{ramp}),0,1))"
    for a, b, cut in ducks:  # full depth across the sentence, ramped in the gaps beside it
        duck = f"min({duck},1-{1 - 10 ** (-cut / 20):.6f}*clip(min((t-{a - ramp:.3f})/{ramp},({b + ramp:.3f}-t)/{ramp}),0,1))"
    return (f"{BED_FILTERS},asetnsamples=n=256:p=0,"
            f"volume='{10 ** (gain / 20):.6f}*(1+{boost:.4f}*{rise})*{duck}':eval=frame")


def bed_chain(narration: Path, bed: Path, spans: list[tuple[float, float]], total_s: float,
              swell_db: float = SWELL_DB) -> tuple[str, float, list[tuple[float, float, float]]]:
    """The bed's filters, a gain that puts it SPEECH_OVER_BED_LU under the voice inside the voiced spans (both
    measured after their filters), and a swell_db rise with SWELL_RAMP_S ramps in each gap. The gain is then capped
    so the bed's loudest 400 ms stays BED_HEADROOM_LU under the voice: a bed composed quiet under speech and loud in the
    gaps is never lifted over the narrator (Plan 2c final review I2). Then any sentence still under SENTENCE_FLOOR_LU
    over the bed gets the bed ducked under it alone, by its shortfall (9 Nov: a swell under one quiet line failed a
    finished video at 9.5 LU). Returns (chain, gain, ducks as (start, end, dB))."""
    voice = _lufs_in(narration, VOICE_CHAIN, spans)
    gain = voice - SPEECH_OVER_BED_LU - _lufs_in(bed, BED_FILTERS, spans)
    if (over := _loudest(bed, _chain(gain, spans, total_s, swell_db)) - (voice - BED_HEADROOM_LU)) > 0:
        gain -= over
    ducks = [(a, b, round(cut, 2)) for a, b in spans if b - a >= MIN_SENTENCE_S
             if (cut := SENTENCE_FLOOR_LU - (_lufs_in(narration, VOICE_CHAIN, [(a, b)])
                                             - _lufs_in(bed, BED_FILTERS, [(a, b)]) - gain)) > 0]
    return _chain(gain, spans, total_s, swell_db, ducks), round(gain, 2), ducks


def _cue(c) -> dict:
    """A cue as the sound writer writes it, or the palette's old (path, start) pair at -12 dB (Plan 2d Task 3)."""
    c = {"path": c[0], "start": c[1]} if isinstance(c, (tuple, list)) else dict(c)
    return {"gain_db": -12.0, "fade_in_s": 0.0, "fade_out_s": 0.0, "pitch_st": 0.0, "loop_s": 0.0, **c,
            "path": Path(c["path"]), "start": float(c["start"])}


def _cue_input(c: dict) -> list[str]:
    return (["-stream_loop", "-1"] if c["loop_s"] else []) + ["-i", str(c["path"])]


def _cue_chain(c: dict) -> str:
    """Pitch by asetrate (an octave up halves the length), a loop trimmed to loop_s, fades, then the delay and the gain."""
    rate, parts = 2 ** (c["pitch_st"] / 12), [f"aresample={SR}", "aformat=sample_fmts=fltp:channel_layouts=stereo"]
    if c["pitch_st"]:
        parts += [f"asetrate={SR * rate:.0f}", f"aresample={SR}"]
    if c["loop_s"]:
        parts.append(f"atrim=duration={c['loop_s']:.3f}")
    if c["fade_in_s"]:
        parts.append(f"afade=t=in:d={c['fade_in_s']:.3f}")
    if c["fade_out_s"]:
        length = c["loop_s"] or seconds(c["path"]) / rate
        parts.append(f"afade=t=out:st={max(0.0, length - c['fade_out_s']):.3f}:d={c['fade_out_s']:.3f}")
    return ",".join(parts + [f"adelay={int(round(c['start'] * 1000))}:all=1", f"volume={c['gain_db']:.2f}dB"])


def _cues_over_voice(narration: Path, cues: list[dict], spans: list[tuple[float, float]]) -> float | None:
    """The cues alone, mixed and measured during speech: their loudest 400 ms minus the voice's loudness (LU)."""
    if not cues:
        return None
    total = seconds(narration)
    graph = ";".join([f"[{i}:a]{_cue_chain(c)}[c{i}]" for i, c in enumerate(cues)]
                     + ["".join(f"[c{i}]" for i in range(len(cues))) + f"amix=inputs={len(cues)}:duration=longest:"
                        f"normalize=0,apad=whole_dur={total:.3f}[out]"])
    with tempfile.TemporaryDirectory() as td:
        g, track = Path(td) / "cues.txt", Path(td) / "cues.wav"
        g.write_text(graph, encoding="utf-8")
        _ffmpeg(["-y", *(a for c in cues for a in _cue_input(c)), "-filter_complex_script", str(g), "-map", "[out]",
                 "-t", f"{total:.3f}", "-c:a", "pcm_s16le", str(track)])
        keep = "+".join(f"between(t,{a:.3f},{b:.3f})" for a, b in spans)
        err = _ffmpeg(["-i", str(track), "-af", f"aselect='{keep}',ebur128", "-f", "null", "-"]).stderr
    peak = max((float(x) for x in re.findall(r"\bM:\s*(-?[\d.]+)", err[:err.rindex("Summary:")])), default=-120.0)
    return round(peak - _lufs_in(narration, VOICE_CHAIN, spans), 2)


def mix_master(narration: Path, bed: Path, cues: list, out_wav: Path, *,
               speech_spans: list[tuple[float, float]], target_lufs: float = -14.0, swell_db: float = SWELL_DB) -> dict:
    """Voice high-passed at 75 Hz; the bed filtered, set SPEECH_OVER_BED_LU under the voice in its voiced spans and
    swelling in the gaps (bed_chain); each cue placed at its time with its gain, fades, pitch and loop (_cue_chain);
    then a two-pass, linear loudnorm to target_lufs / MASTER_TP. Returns the quietest sentence's speech-over-bed gap,
    the bed's own mono fold-down loss, how far its loudest 400 ms sit under the voice, its gain (Plan 2c final review
    I1/I2: each of these can fail), and the cues' loudest 400 ms during speech against the voice (Plan 2d)."""
    if not speech_spans:
        raise ValueError("mix_master needs the narration's voiced spans")
    chain, gain, ducks = bed_chain(narration, bed, speech_spans, seconds(narration), swell_db)
    cues = [_cue(c) for c in cues]
    inputs = [narration, bed, *(c["path"] for c in cues)]
    chains = [f"[0:a]aresample={SR},aformat=sample_fmts=fltp:channel_layouts=stereo,{VOICE_CHAIN}[a0]",
              f"[1:a]aresample={SR},aformat=sample_fmts=fltp:channel_layouts=stereo,{chain}[a1]"]
    chains += [f"[{i}:a]{_cue_chain(cues[i - 2])}[a{i}]" for i in range(2, len(inputs))]
    graph = ";".join(chains + ["".join(f"[a{i}]" for i in range(len(inputs)))
                               + f"amix=inputs={len(inputs)}:duration=first:normalize=0[mix]"])
    args_in = ["-i", str(narration), "-i", str(bed), *(a for c in cues for a in _cue_input(c))]
    norm = f"loudnorm=I={target_lufs}:TP={MASTER_TP}:LRA=11"
    with tempfile.TemporaryDirectory() as td:  # graphs in files: many cues would overflow the Windows command line
        g1, g2 = Path(td) / "pass1.txt", Path(td) / "pass2.txt"
        g1.write_text(f"{graph};[mix]{norm}:print_format=json[out]", encoding="utf-8")
        err = _ffmpeg([*args_in, "-filter_complex_script", str(g1), "-map", "[out]", "-f", "null", "-"]).stderr
        m = json.loads(err[err.rindex("{"): err.rindex("}") + 1])
        g2.write_text(f"{graph};[mix]{norm}:measured_I={m['input_i']}:measured_TP={m['input_tp']}:"
                      f"measured_LRA={m['input_lra']}:measured_thresh={m['input_thresh']}:offset={m['target_offset']}:"
                      f"linear=true,aresample={SR}[out]", encoding="utf-8")
        _ffmpeg(["-y", *args_in, "-filter_complex_script", str(g2), "-map", "[out]", "-ar", str(SR),
                 "-c:a", "pcm_s16le", str(out_wav)])
    gaps = [_lufs_in(narration, VOICE_CHAIN, [s]) - _lufs_in(bed, chain, [s]) for s in speech_spans if s[1] - s[0] >= MIN_SENTENCE_S]
    voiced = min(gaps) if gaps else _lufs_in(narration, VOICE_CHAIN, speech_spans) - _lufs_in(bed, chain, speech_spans)
    whole = [(0.0, 1e6)]  # the bed alone: on the master the voice hid a bed that cancels (I1)
    mono = _lufs_in(bed, chain, whole) - _lufs_in(bed, f"{chain},{FOLD}", whole)
    headroom = _lufs_in(narration, VOICE_CHAIN, speech_spans) - _loudest(bed, chain)
    return {"speech_to_bed_lu": round(voiced, 2), "mono_loss_db": round(mono, 2), "bed_gain_db": gain,
            "ducked": [list(d) for d in ducks],
            "bed_peak_under_voice_lu": round(headroom, 2),
            "loudest_cue_over_speech_lu": _cues_over_voice(narration, cues, speech_spans)}


def loudness(path: Path) -> tuple[float, float]:
    """(integrated LUFS, true peak dBTP), measured with ebur128 on the file as delivered."""
    err = _ffmpeg(["-i", str(path), "-map", "0:a:0", "-af", "ebur128=peak=true", "-f", "null", "-"]).stderr
    summary = err[err.rindex("Summary:"):]
    return (float(re.search(r"I:\s+(-?[\d.]+) LUFS", summary).group(1)),
            float(re.search(r"True peak:\s+Peak:\s+(-?[\d.]+) dBFS", summary).group(1)))


def _loudest(path: Path, chain: str) -> float:
    """The highest momentary (400 ms) loudness of path through chain, from ebur128's running log. A 3 s window
    averaged a 1.5 s gap phrase with the quiet bed beside it and let it land 3 LU from the voice."""
    err = _ffmpeg(["-i", str(path), "-af", f"aresample={SR},aformat=sample_fmts=fltp:channel_layouts=stereo,{chain},"
                   "ebur128", "-f", "null", "-"]).stderr
    values = [float(x) for x in re.findall(r"\bM:\s*(-?[\d.]+)", err[:err.rindex("Summary:")])]
    return max(values, default=-120.0)


def _lufs_in(path: Path, chain: str, spans: list[tuple[float, float]]) -> float:
    keep = "+".join(f"between(t,{a:.3f},{b:.3f})" for a, b in spans)
    err = _ffmpeg(["-i", str(path), "-af", f"aresample={SR},aformat=sample_fmts=fltp:channel_layouts=stereo,{chain},"
                   f"aselect='{keep}',ebur128", "-f", "null", "-"]).stderr
    return float(re.search(r"I:\s+(-?[\d.]+) LUFS", err[err.rindex("Summary:"):]).group(1))
