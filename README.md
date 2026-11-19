# autovideo

An automated, local-first video generation pipeline designed for high-cadence short-form and forensic explainer content.

## Architecture

`autovideo` orchestrates video creation through a modular, staged pipeline:

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
[Narrate]  ─── Voice synthesis (Gemini TTS / Voice Cloning) + WhisperX alignment
    │
    ▼
 [Scene]   ─── HyperFrames composition / 3D web scene assembly with design tokens
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

## Operator Console

The operator console provides a local web UI for inspecting and managing video production:

```bash
python -m factory console
```

- **Home**: Real-time status board of videos across production stages (Being made, Ready to post, On air).
- **Video Detail**: Frame inspection, timing graphs, stage logs, and manual overrides.
- **Settings**: Per-stage model selection, effort parameters, quota thresholds, and fallbacks.

The frontend is built with React, TypeScript, Vite, and Tailwind CSS in `factory/console/web`.

## Multi-Provider LLM Engine

The factory supports dynamic model routing across multiple providers via `factory/llm`:
- **Claude** (Anthropic API / Claude CLI)
- **Google Gemini** (Gemini API with thinking levels)
- **OpenAI-Compatible APIs** (DeepSeek, Qwen, local LLMs)

## Requirements

- Python 3.10+
- FFmpeg (added to PATH)
- Node.js 18+ (for HyperFrames rendering)
