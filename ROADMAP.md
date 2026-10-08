# Roadmap

Coding agents must read this file before selecting work. Specs live in `docs/roadmap/`. Do not implement later items while an earlier one is active unless a minimal prerequisite is unavoidable; record that decision here.

## Active

**003** — Web dashboard (`docs/roadmap/003-web-dashboard.md`) — **IN PROGRESS**

## Status

| Item | Spec | Status |
|---|---|---|
| 000 ASR quality: conservative duplicate removal | `docs/roadmap/000-asr-quality-dedup.md` | **DONE** (`19fabc7`). Residual: English can still emit `means` + `means?` because `_normalize_token` does not strip punctuation. Preview cue: “Sabe o que isso significa?”. `means means` → translation is PARTIALLY met. |
| 001 Automatic processing service | `docs/roadmap/001-auto-processing.md` | **DONE**. Daemon command, reconciliation scan, sidecar auto-publish under `publish_mode: sidecar`, restart recovery of `running` jobs, skip existing `.pt-BR.srt`. Default config remains `publish_mode: staging`. Residual: `stability_window_seconds` / `minimum_file_age_seconds` stay 600 in the example config (not the spec's illustrative 60); publication stays exclusive `os.link` (never overwrite). |
| 002 Language detection and selection | `docs/roadmap/002-language-intelligence.md` | **DONE**. Nested `languages`/`audio` config (`source: auto`, `target: pt-BR`, preferred streams `en` then `ja`); auto detection always samples Whisper and only trusts metadata when it agrees; low confidence → `needs_review`; `pt`/`pt-BR` skip translation by public family (Argos `pb` stays internal); decisions persist on the manifest. Residual: target remains a single `pt-BR` (005); a Japanese-preferred stream still fails as `unsupported_language`. |
| 003 Web dashboard | `docs/roadmap/003-web-dashboard.md` | **IN PROGRESS**. Separate `nas-subs dashboard` process and `compose.dashboard.yaml` service over the existing repository/scan/transition APIs. Worker lock is not taken. Residual: settings are read-only; no remote/public authentication beyond an optional shared token. |
| 004 Sonarr/Radarr webhooks | `docs/roadmap/004-sonarr-radarr-webhooks.md` | PLANNED |
| 005 Multiple target languages | `docs/roadmap/005-multiple-target-languages.md` | PLANNED |
