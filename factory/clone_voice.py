"""A channel's cloned narrator (design 8.5 item 1; FAILURE_MODES.md, voice clone and clone phrasing). factory/voice.py
runs it inside voice-clone-env, which holds Chatterbox, Matchering, Praat and librosa:

    <voice-clone-env>/Scripts/python factory/clone_voice.py --input IN.json --out WAV --prompt P --real-vocals V --tune T

IN.json is {"beats": [{"text", "role"}], "phrasing": the workspace's voice.phrasing}. Chatterbox (MIT) speaks one take
per sentence from the primary reference clip, at its story role's intensity; each take is trimmed, checked and redone
up to 3 times, and the takes are joined with pauses by story beat. Matchering matches EQ and loudness to the real
narration. Praat PSOLA moves the pitch median onto the real narrator's, keeps the takes' own range, and speeds the
talking up by phrasing.speed; pauses go in longer by the same factor, so they come out as set. Prints one JSON line."""
import argparse
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WPS = 3.0  # the clone's own pace in words a second: a take's length is checked against what its words need at it
TRIES = 3


def takes_of(text: str) -> list[str]:
    """Sentences; one of 3 words or fewer rides with its neighbour, because a take that short can come out empty."""
    out = []
    for s in (s.strip() for s in re.split(r"(?<=[.!?])\s+|\n+", text) if s.strip()):
        if out and (len(s.split()) <= 3 or len(out[-1].split()) <= 3):
            out[-1] += " " + s
        else:
            out.append(s)
    return out


def plan(beats: list[dict], phrasing: dict, seed_offset: int = 0) -> list[dict]:
    """One take per sentence: its text, the pause before it, its role's intensity and a seed fixed by its place (plus
    seed_offset: a voice-check retry reads with fresh seeds, since the same seeds repeat the same misreads)."""
    out = []
    for i, b in enumerate(beats):
        ex, cfg = phrasing["roles"].get(b.get("role"), phrasing["default"])
        for j, text in enumerate(takes_of(b["text"])):
            pause = ("sentence" if j else None if not i else "last" if i == len(beats) - 1 else
                     "twist" if b.get("role") == "flip" and beats[i - 1].get("role") != "flip" else "beat")
            out.append({"text": text, "pause": pause, "exaggeration": ex, "cfg_weight": cfg,
                        "seed": seed_offset + 1000 * i + 10 * j})
    return out


def take(model, prompt: str, step: dict):
    """The best of up to 3 tries, trimmed, 0.5-1.9 times as long as its words need. pYIN must find 30% of it voiced:
    a take it calls breathy is redone. Praat is the floor: a take it finds under 20% voiced is a whisper, and fails
    (pYIN alone read voiced, slightly breathy takes as 0% voiced, 6 Nov)."""
    import librosa
    import numpy as np
    import parselmouth
    import torch
    sr, want, best = model.sr, len(step["text"].split()) / WPS, None
    for n in range(TRIES):
        torch.manual_seed(step["seed"] + n)
        y = model.generate(step["text"], audio_prompt_path=prompt, exaggeration=step["exaggeration"],
                           cfg_weight=step["cfg_weight"]).cpu().numpy()[0]
        _, (a, b) = librosa.effects.trim(y, top_db=40)
        y = y[max(0, a - int(0.04 * sr)):b + int(0.04 * sr)]  # keep 40 ms so a soft consonant isn't clipped
        voiced = 0.0 if len(y) < 0.1 * sr else float(np.mean(librosa.pyin(
            librosa.resample(y, orig_sr=sr, target_sr=16000), fmin=65, fmax=500, sr=16000)[1]))
        f0 = parselmouth.Sound(y.astype(np.float64), sampling_frequency=sr).to_pitch(
            pitch_floor=65, pitch_ceiling=500).selected_array["frequency"] if len(y) >= 0.1 * sr else np.zeros(1)
        seconds, praat = max(len(y), 1) / sr, float(np.mean(f0 > 0))
        fits = 0.5 * want <= seconds <= 1.9 * want
        ok, usable = voiced >= 0.3 and fits, praat >= 0.2 and fits
        score = (not ok) + 5 * (not usable) + abs(np.log(seconds / want))
        if best is None or score < best[0]:
            best = (score, y, {"ok": ok, "usable": usable, "voiced": round(voiced, 2), "voiced_praat": round(praat, 2),
                               "seconds": round(seconds, 2)})
        if ok:
            break
    return best[1], {"text": step["text"], "tries": n + 1, **best[2]}


def pitch(path: Path) -> tuple[float, float]:
    """(median, p90-p10 spread) in semitones, by pYIN, as voice_tune.py measured the real narrator."""
    import librosa
    import numpy as np
    y, sr = librosa.load(str(path), sr=16000, mono=True)
    f0, voiced, _ = librosa.pyin(y, fmin=65, fmax=500, sr=sr)
    semis = 12 * np.log2(f0[voiced & ~np.isnan(f0)] / 440.0) + 69
    return float(np.median(semis)), float(np.percentile(semis, 90) - np.percentile(semis, 10))


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--input", type=Path, required=True, help='{"beats": [{"text", "role"}], "phrasing": {...}}')
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--prompt", type=Path, required=True, help="the reference clip Chatterbox clones")
    ap.add_argument("--real-vocals", type=Path, required=True, help="real vocals Matchering matches")
    ap.add_argument("--tune", type=Path, required=True, help="tune.json with the real narrator's pitch")
    a = ap.parse_args()
    if missing := [str(p) for p in (a.prompt, a.real_vocals, a.tune) if not p.exists()]:
        sys.exit(f"missing clone inputs (local only, git-ignored): {missing}")
    import numpy as np
    import torch
    import torchaudio as ta
    from chatterbox.tts import ChatterboxTTS
    spec = json.loads(a.input.read_text(encoding="utf-8"))
    phrasing, steps = spec["phrasing"], plan(spec["beats"], spec["phrasing"], spec.get("seed_offset", 0))

    def render(device: str):
        model = ChatterboxTTS.from_pretrained(device=device)
        return [take(model, str(a.prompt), s) for s in steps], model.sr

    device = "cuda" if torch.cuda.is_available() else "cpu"
    try:
        takes, sr = render(device)
    except torch.cuda.OutOfMemoryError:  # the 4 GB GPU: fall back to CPU, slower
        torch.cuda.empty_cache()
        device = "cpu"
        takes, sr = render(device)
    speed, parts = phrasing["speed"], []
    for step, (y, _) in zip(steps, takes):
        if step["pause"]:  # Praat shortens everything by 1/speed below: a pause goes in that much longer
            parts.append(np.zeros(int(phrasing["pauses"][step["pause"]] * speed * sr), dtype=np.float32))
        parts.append(y.astype(np.float32))
    clone, matched = a.out.with_suffix(".clone.wav"), a.out.with_suffix(".matched.wav")
    ta.save(str(clone), torch.from_numpy(np.concatenate(parts))[None], sr)
    import matchering as mg
    mg.process(target=str(clone), reference=str(a.real_vocals), results=[mg.pcm16(str(matched))])
    import parselmouth
    from parselmouth.praat import call
    target = json.loads(a.tune.read_text(encoding="utf-8"))["real_narrator"]
    voice = parselmouth.Sound(str(matched)).convert_to_mono()  # Matchering writes stereo; Change gender needs mono
    ask, tried, best = target["pitch_median_st"], [], None
    for n in range(1, 5):  # Praat's tracker and pYIN disagree: steer the asked median by the measured residual
        shifted = call(voice, "Change gender", 75, 600, 1.0, 440.0 * 2 ** ((ask - 69) / 12), 1.0, 1 / speed)
        shifted.scale_peak(0.89)  # -1 dBFS: PSOLA can push peaks past full scale
        trial = a.out.with_name(f"{a.out.stem}.pass{n}.wav")
        shifted.save(str(trial), "WAV")
        residual = pitch(trial)[0] - target["pitch_median_st"]
        tried.append((ask, residual))
        if best is None or abs(residual) < abs(best[1]):
            best = (trial, residual)
        if abs(residual) < 0.25:
            break
        # pYIN can move more than the semitone asked (6 Nov, a full episode: 1.85, -0.85, 0.35, -0.35 st, about
        # 1.4 per 1), so plain steps swing past. After two passes, step by the measured slope (a secant step); a
        # slope outside 0.5-3 is noise, so the step stays plain.
        (a0, r0), (a1, r1) = tried[-2:] if n > 1 else [(0.0, 0.0), (1.0, 1.0)]
        slope = (r1 - r0) / (a1 - a0) if a1 != a0 else 1.0
        ask -= residual / (slope if 0.5 <= slope <= 3 else 1.0)
    residuals = [round(r, 3) for _, r in tried]
    best[0].replace(a.out)  # keep the closest pass: pYIN on a short clip is noisy, so later isn't always better
    for p in a.out.parent.glob(f"{a.out.stem}.pass*.wav"):
        p.unlink()
    clone.unlink()
    matched.unlink()
    checks = [info for _, info in takes]
    print(json.dumps({"device": device, "passes": len(residuals), "residuals": residuals,
                      "asked_st": [round(x, 2) for x, _ in tried],
                      "pitch_residual_st": round(best[1], 3), "speed": speed, "takes": len(checks),
                      "retries": sum(c["tries"] - 1 for c in checks),
                      "takes_failed": [c["text"] for c in checks if not c["usable"]], "take_checks": checks}))


if __name__ == "__main__":
    main()
