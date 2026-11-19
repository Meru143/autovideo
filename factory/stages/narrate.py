"""narrate: the workspace's voice reads narration.txt, and the offline transcript becomes timing.json, the clock every
scene beat, caption and sound cue syncs to (design 5.5.2 steps 7-8; Plan 2b Task 6; FAILURE_MODES.md, narrate)."""
from __future__ import annotations

import json
import shutil

from ..jobs import Job, Quarantined
from ..voice import VoiceCheckFailed, synthesize


def build_timing(transcript: dict, audio_s: float) -> dict:
    """{words, sentences, duration_s, audio_duration_s}. Words WhisperX couldn't time get times spread evenly between
    their timed neighbours, so every heard word is on the clock, in order."""
    raw = [w for s in transcript.get("segments", []) for w in s.get("words", [])]
    if not any(w.get("start") is not None and w.get("end") is not None for w in raw):
        raise RuntimeError("the transcript has no timed words to sync to")
    words, i = [], 0
    while i < len(raw):
        w = raw[i]
        if w.get("start") is not None and w.get("end") is not None:
            words.append({"w": w["word"].strip(), "start": float(w["start"]), "end": float(w["end"])})
            i += 1
            continue
        j = i
        while j < len(raw) and (raw[j].get("start") is None or raw[j].get("end") is None):
            j += 1  # a run of untimed words: raw[i:j]
        lo = words[-1]["end"] if words else 0.0
        hi = float(raw[j]["start"]) if j < len(raw) else audio_s
        step = max(hi - lo, 0.0) / (j - i)
        for k in range(i, j):
            start = lo + step * (k - i)
            words.append({"w": raw[k]["word"].strip(), "start": round(start, 3), "end": round(start + step, 3)})
        i = j
    sentences, current = [], []
    for w in words:
        current.append(w)
        if w["w"].rstrip("\"'”’").endswith((".", "?", "!")):  # 'up."' ends a sentence; 'it,"' doesn't
            sentences.append(current)
            current = []
    if current:
        sentences.append(current)
    return {"words": words, "duration_s": words[-1]["end"], "audio_duration_s": audio_s,
            "sentences": [{"text": " ".join(x["w"] for x in s), "start": s[0]["start"], "end": s[-1]["end"]}
                          for s in sentences]}


def narrate(job: Job) -> dict:
    text = (job.dir / "narration.txt").read_text(encoding="utf-8")
    vdir = job.dir / "voice"
    ep = job.dir / "episode.json"  # a story's beats give a cloned voice its pauses and intensity by role
    beats = json.loads(ep.read_text(encoding="utf-8")).get("beats") if ep.exists() else None
    if beats and " ".join(b["text"] for b in beats).split() != text.split():
        beats = None  # narration.txt changed after the story stage: read it as written
    for attempt in (1, 2):  # TTS variance can fix a misread once; a second miss is the script's or the lexicon's
        try:
            result = synthesize(job.workspace, text, vdir, beats=beats, attempt=attempt)
            break
        except VoiceCheckFailed as e:
            if attempt == 2:
                raise Quarantined(f"voice checks failed twice: {e.findings}") from None
    transcript = json.loads((vdir / "_asr" / "transcript.json").read_text(encoding="utf-8"))
    timing = build_timing(transcript, result["seconds"])
    (job.dir / "timing.json").write_text(json.dumps(timing, indent=2), encoding="utf-8")
    shutil.copyfile(vdir / "narration.wav", job.dir / "narration.wav")
    return {"seconds": result["seconds"], "rate": result["rate"], "wer": result["wer"],
            "words": len(timing["words"]), "sentences": len(timing["sentences"])}
