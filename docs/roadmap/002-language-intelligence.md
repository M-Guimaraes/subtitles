# 002 — Language Detection and Selection

**Status:** DONE

## Goal

Support automatic source-language detection, configurable target language, and intelligent audio-stream selection without hardcoding English → PT-BR.

## Default experience

```text
source_language: auto
target_language: pt-BR
```

The application inspects audio streams, chooses the intended stream, determines source language with confidence, transcribes it, and translates only when required.

## Audio stream inspection

Use ffprobe metadata to enumerate audio streams, including index, language tag, title/name, codec and channel information where available.

Do not blindly trust metadata: missing/unknown language is common. Metadata is a candidate signal, not the only source of truth.

## Detection

faster-whisper already returns detected language and probability. Persist these on the job/result.

If source language is explicitly configured, respect the override. With `auto`, combine selected stream metadata and Whisper detection using a documented deterministic policy.

If confidence is below a defined threshold, do not silently make destructive assumptions. Record low-confidence state and apply the configured fallback/review policy.

## Source equals target

If the source language is effectively the requested target language, skip translation and generate the SRT directly from transcription. Language equivalence/locale handling must be explicit; do not compare backend codes naively.

## Configuration direction

```yaml
languages:
  source: auto
  target: pt-BR

audio:
  stream: auto
  preferred_languages:
    - en
    - ja
```

Exact schema should follow existing project conventions.

## Public vs backend identifiers

Public configuration and filenames use logical identifiers such as `en`, `ja`, `pt-BR`. Backend mappings remain internal. Existing Argos mapping `pt-BR -> pb` must continue working without exposing `pb` publicly.

## Persisted metadata

Jobs/results should make it possible to display at least:

- selected audio stream index;
- stream language metadata;
- detected language;
- detection probability;
- source-language decision;
- target language;
- whether translation was executed/skipped;
- ASR/translation model identity.

## Tests

Cover explicit source override; automatic English detection; missing stream language metadata; source=target translation bypass; PT-BR backend mapping remains internal; low-confidence behavior; deterministic audio-stream selection; language decision participates in cache/pipeline identity where necessary.

## Acceptance

A media file can be processed with `source=auto` and `target=pt-BR`; the detected source and confidence are persisted; English content translates to PT-BR; Portuguese content can produce PT-BR subtitles without unnecessary translation; public filenames remain `.pt-BR.srt`.
