# 000 — ASR Quality: Conservative Duplicate Removal

**Status:** DONE (implementation shipped in `19fabc7050e166e7d35a7bd09252640e37a13c69`)

**Residual:** English can still emit `means` + `means?` because `_normalize_token` does not strip punctuation. The 300-second `medium` Dexter preview Portuguese cue became “Sabe o que isso significa?”. Acceptance that translation must not receive `means means` is **PARTIALLY** met.

## Goal

Remove clearly spurious consecutive duplicate words produced by faster-whisper without deleting legitimate repeated speech.

## Real reproduction

Dexter S03E01, first 300 seconds:

- `means` 19.50–19.80, probability ~0.9986
- `means` 19.80–20.28, probability ~0.3833

The original cue included `Do you know what that means means?`. After this change, the collapsed high/low pair is gone, but a punctuated twin (`means?`) is still a different normalized token, so English can still show `means means?`. Argos collapsed the preview to **“Sabe o que isso significa?”**.

Measured preview sidecar (not in git; `data/` is ignored):

`data/output/media-c5021229/Dexter.S03E01.1080p.5.1Ch.BluRay.ReEnc-DeeJayAhmed.pt-BR.preview.srt`

## Existing behavior (preserved)

`_dedupe_overlapping_words()` still removes equivalent tokens when their time intervals **truly overlap** (`min(end) > max(start)`) and keeps the higher-confidence occurrence. Touching-but-not-overlapping tokens are not treated as overlap. That path is unchanged.

## Shipped consecutive-duplicate rule

Applied in `_dedupe_overlapping_words()` after the overlap collapse, during `merge_chunk_transcripts`. For immediately adjacent equivalent normalized tokens, the pair is treated as a likely ASR duplicate only when **all** of:

- `_normalize_token()` values are equal (`text.strip().casefold()`; **punctuation is not stripped**);
- `abs(previous.end_seconds - word.start_seconds) <= BOUNDARY_EPSILON_SECONDS` (`0.02`);
- both confidence values are present (`None` is never high or low);
- one probability is `>= HIGH_CONFIDENCE` (`0.80`);
- the other is `< LOW_CONFIDENCE` (`0.60`) — not `<= 0.60`.

Keep the higher-confidence token (`_higher_confidence`). Missing probability is not treated as low confidence. High/high repetition such as `very very` is kept. A gap larger than epsilon is kept. Adjacent different words are never dropped.

## Public API and identity

- Heuristic runs inside **`merge_chunk_transcripts`** (the tested public surface). Tests do not import `_dedupe_overlapping_words`.
- Module constants: `HIGH_CONFIDENCE = 0.80`, `LOW_CONFIDENCE = 0.60`, `BOUNDARY_EPSILON_SECONDS = 0.02`.
- `TRANSCRIBE_WORD_DEDUPE_VERSION = 1` in `domain.py`. Included as `"word_dedupe"` in `AppConfig.pipeline_config_hash` and `stage_config_hash(PipelineStage.TRANSCRIBE)` so existing TRANSCRIBE checkpoints are not reused after this heuristic. MERGE hash is unaffected. No global state wipe.

## Tests (shipped)

`tests/unit/test_transcription.py` covers, via `merge_chunk_transcripts`:

- real `means` high/low touching pair dropped;
- high/high `very very` preserved;
- `None`/`None` preserved;
- gap > epsilon preserved;
- adjacent different words preserved;
- existing true-overlap test still passing.

`test_transcribe_hash_includes_word_dedupe_version` in `tests/unit/test_config_schema.py` asserts bumping the version changes TRANSCRIBE and pipeline hashes, not MERGE.

`uv run pytest -m 'not models'` reported **142** passing at ship time.

## Acceptance

- transcription tests pass — **met**;
- full suite (`pytest -m 'not models'`) passes — **met** (142);
- 300-second `medium` Dexter preview no longer sends `means means` to translation — **PARTIAL**: the unpunctuated high/low pair is collapsed; `means` + `means?` can still reach translation because punctuation is not stripped;
- legitimate repetition tests remain intact — **met**;
- no unrelated Whisper/Argos/SRT behavior changes — **met** (this commit touched transcription merge/dedupe, domain version, and config hashes only).

Punctuation-aware normalization was **not** implemented.
