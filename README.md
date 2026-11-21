# autovideo

An automated, local-first video production and clipping pipeline designed for high-cadence short-form, story, and explainer content.

## Pipelines

`autovideo` supports three per-channel pipelines (`explainer`, `story`, and `clip`):

### 1. Generative Explainer & Story Pipeline

```
Brief / Topic
    │
    ▼
[Research] ─── Multi-source evidence gathering and structured fact extraction
    │
    ▼
 [Script]  ─── Hook generation, cadence budgeting, fact placement, script lint
    │
    ▼
[Narrate]  ─── Voice synthesis (Gemini TTS / Voice Cloning) + Whisper alignment
    │
    ▼
 [Scene]   ─── Web / 3D scene assembly with design tokens
    │
    ▼
[Render]   ─── Headless Chrome frame-by-frame rendering with layout verification
    │
    ▼
[Review]   ─── Automated visual audit, OCR check, collision detection & repair
    │
    ▼
 [Master]  ─── Audio mixing, loudness normalization (LUFS), and MP4 multiplexing
```

### 2. Short-Form VOD & Live Stream Clip Pipeline (`factory/clip/`)

- **VOD Ingest** (`python -m factory clip ingest <channel> <url>`): Downloads YouTube/Twitch VODs (or local files) with automatic audio-first + targeted 1080p section slicing for long VODs (>2h), local GPU/CPU `faster-whisper` transcription, multi-signal highlight scoring (YouTube heatmaps, Twitch VOD chat spikes, audio hype spikes), and window deduplication.
- **Stream & Trend Radar** (`python -m factory clip radar --query "<topic>"`): Keyless discovery of live Twitch/YouTube streams and fastest-rising YouTube videos.
- **Live Chat-Spike Recorder** (`python -m factory clip live <url> --channel <channel>`): Keyless Twitch IRC / YouTube live chat monitoring with rolling zero-reencode MPEG-TS recording, LLM quality gating, and automatic clip job queuing.
- **Smart Rendering & Captions** (`factory/clip/engine/`): YuNet face tracking, automatic talking-head / split-screen webcam framing, tight or natural pacing, 14 ASS caption presets, and `-14 LUFS` loudness normalization.
- **Publishing & Engagement**: Scheduled YouTube Shorts and Instagram Reels uploads with optional auto-commenting of generated `pinned_comment`, prepublic safety checks (with operator NSFW override), and post-upload media cleanup.

## Operator Console

The operator console provides a local web UI (`127.0.0.1` only) for inspecting and managing video production:

```bash
python -m factory console
```

- **Home**: Real-time board across `Being made`, `Ready to post`, and `On air`, live VOD ingest & watcher progress cards, log tails, and `Run now` / `Post now` controls.
- **Video Detail**: Video player, stage timeline, interactive **Clip Editor** (trim start/end at playhead, framing, pacing, speed, caption presets, inline caption editing, and re-render), title/caption/pinned-comment editor, and one-click NSFW override.
- **Channel**: Schedule, slot & cap controls, playbook editor with git diff/Undo, and **Radar & Watch Live** controls.
- **Settings**: Concurrency, retries, quota limits, and multi-provider LLM routing.

The frontend is built with React, TypeScript, Vite, and Tailwind CSS in `factory/console/web`.

## Multi-Provider LLM Engine

`factory/llm` supports protocol-driven model routing with automatic failover via `secrets/providers.json`:

- **Claude** (`claude` — Claude CLI)
- **Google Gemini** (`"protocol": "gemini"` — native REST API; reads `env_key` from `secrets/providers.json`, defaulting to `GEMINI_API_KEY`)
- **OpenAI & OpenAI-Compatible APIs** (`"protocol": "openai"` — OpenAI, DeepSeek, OpenRouter, Ollama, vLLM, or any custom endpoint with `base_url` and `env_key`)
- **Local CLI Runners** (`"protocol": "cli"` — any compatible CLI binary configured with `executable` and `model` in `secrets/providers.json`)

## Configuration (Zero Engine Edits Required)

All channel-specific settings, niche queries, and provider credentials are decoupled from engine code:

- `channels/<name>/settings.json` — channel schedule, playbooks, publishing consent, and optional `clip` settings (`auto_watch_streamers`, `auto_watch_query`, `auto_watch_keywords`, `live_niche`, `title_stop_words`, `caption_style`, `default_layout`)
- `secrets/providers.json` & `secrets/*.env` — LLM provider routing, API keys, and OAuth tokens
- `factory/settings.json` — global concurrency, retry, and pause controls (paused by default on fresh clones)

## YouTube Channel OAuth

To authorize a channel for YouTube uploads and analytics (requires `secrets/google_oauth_client.json`):

```bash
python tools/youtube_auth.py <channel>
```

## Requirements

- Python 3.10+
- FFmpeg & FFprobe (on `PATH`)
- `yt-dlp` (for VOD and live stream clipping)
- Node.js 18+ (for web/3D scene rendering and building the console frontend)
