# 005 — Multiple Target Languages

**Status:** BACKLOG

## Goal

Allow one media item to produce multiple independently tracked subtitle targets, e.g. PT-BR and English.

## Direction

Evolve the single `target_language` model into ordered/configurable target languages without breaking existing single-target configuration unnecessarily.

Each target must have independent publication identity/status so one failed translation does not invalidate successful targets.

Expected filenames remain logical-language based:

- `episode.pt-BR.srt`
- `episode.en.srt`
- `episode.es.srt`

Reuse source transcription whenever safe instead of retranscribing the same audio for every target.

## Acceptance

One transcription can feed multiple target outputs; each output is idempotently tracked and published; existing single-target deployments have a documented migration/default behavior.
