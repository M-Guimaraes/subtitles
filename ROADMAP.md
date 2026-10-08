# Roadmap

Coding agents must read this file before selecting work. Specs live in `docs/roadmap/`. Do not implement later items while an earlier one is active unless a minimal prerequisite is unavoidable; record that decision here.

## Active

**001** — Automatic processing service (`docs/roadmap/001-auto-processing.md`) — **PLANNED**

## Status

| Item | Spec | Status |
|---|---|---|
| 000 ASR quality: conservative duplicate removal | `docs/roadmap/000-asr-quality-dedup.md` | **DONE** (`19fabc7`). Residual: English can still emit `means` + `means?` because `_normalize_token` does not strip punctuation. Preview cue: “Sabe o que isso significa?”. `means means` → translation is PARTIALLY met. |
| 001 Automatic processing service | `docs/roadmap/001-auto-processing.md` | PLANNED (next) |
| 002 Language detection and selection | `docs/roadmap/002-language-intelligence.md` | PLANNED |
| 003 Web dashboard | `docs/roadmap/003-web-dashboard.md` | PLANNED |
| 004 Sonarr/Radarr webhooks | `docs/roadmap/004-sonarr-radarr-webhooks.md` | PLANNED |
| 005 Multiple target languages | `docs/roadmap/005-multiple-target-languages.md` | PLANNED |
