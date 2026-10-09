# 005 — Multiple Target Languages

**Status:** DONE

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

## Schema

```yaml
languages:
  source: auto
  targets:
    - pt-BR
    - en
```

A legacy nested `languages.target: pt-BR` still loads and is folded into
`targets: [pt-BR]`. If both keys are present they must represent the same
one-item list. Public identifiers only; Argos `pb` is rejected.

## Acceptance

One transcription can feed multiple target outputs; each output is idempotently tracked and published; existing single-target deployments have a documented migration/default behavior.

Implemented: one independent job per configured target, keyed by
`pipeline_config_hash_for(target)` plus the stored public `target_language`.
Source=target skips translation for that job only. A missing local pair fails
that target with `translation_pair_missing` and does not discard siblings.
Existing-sidecar skip is per language. Dashboard and webhook enqueue show the
target on each job.

Residual: each target still transcribes independently (checkpoints are
per-job). Additional Argos pairs are not downloaded by `models install`; a
missing pair is a clear failure. `en -> pt` produces Portuguese and does not
guarantee Brazilian Portuguese.
