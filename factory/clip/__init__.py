"""Short-form stream and VOD clipping pipeline."""
from __future__ import annotations

LAYOUTS = ("auto", "fill", "fit")
PACINGS = ("tight", "natural")
CAPTION_STYLES = (
    "pop", "spotlight", "impact", "glow", "boxed", "sweep",
    "editorial", "hype", "punch", "neon", "headline", "paper", "subtle", "viral",
)


def default_spec(
    *,
    source_url: str,
    source_path: str,
    source_width: int = 1920,
    source_height: int = 1080,
    start_s: float,
    end_s: float,
    title: str = "",
    hook: str = "",
    reason: str = "",
    score: float = 0.0,
    layout: str = "auto",
    caption_style: str = "neon",
    pacing: str = "natural",
    speed: float = 1.0,
    include_captions: bool = True,
    include_title: bool = False,
    captions: list[dict] | None = None,
    normalize_audio: bool = True,
    allow_profanity: bool = False,
    creator: str = "",
    creator_handle: str = "",
    title_stop_words: list[str] | None = None,
) -> dict:
    return {
        "source_url": source_url,
        "source_path": source_path,
        "source_width": int(source_width or 1920),
        "source_height": int(source_height or 1080),
        "start_s": round(float(start_s), 2),
        "end_s": round(float(end_s), 2),
        "title": title,
        "hook": hook,
        "reason": reason,
        "score": round(float(score), 1),
        "layout": layout if layout in LAYOUTS else "auto",
        "caption_style": caption_style if caption_style in CAPTION_STYLES else "neon",
        "pacing": pacing if pacing in PACINGS else "natural",
        "speed": float(speed),
        "include_captions": bool(include_captions),
        "include_title": bool(include_title),
        "captions": list(captions or []),
        "normalize_audio": bool(normalize_audio),
        "allow_profanity": bool(allow_profanity),
        "creator": str(creator or "").strip(),
        "creator_handle": str(creator_handle or "").strip(),
        "title_stop_words": [str(w).strip().lower() for w in (title_stop_words or []) if str(w).strip()],
    }
