# Benchmarks

Nothing in this file is an estimate of how long a personal library would take
on a NAS. `/nas/media` was not mounted while this software was written.

## What was measured

- Host: Apple Silicon (arm64) Mac, Python 3.11 from Homebrew, FFmpeg 9.
- Automated tests: unit and integration suites with synthetic FFmpeg fixtures
  and fake ASR/translation engines. Those tests do **not** measure model
  quality or realtime factor.
- `nas-subs benchmark` will report elapsed time, media seconds and RTF on
  whatever machine you run it on. Record that output here only after it has
  actually been run.

## What was not measured

- Whisper `small` / int8 throughput on this Mac (blocked unless `models install` succeeds).
- Argos `en -> pt` quality on a 20-cue pilot or a real episode.
- Any NAS host, USB enclosure, ZFS pool, amd64, or GPU path.
- End-to-end time for a library of hundreds of episodes.

`en -> pt` produces Portuguese. It does not guarantee Brazilian Portuguese.
The `.pt-BR.srt` suffix is the destination we want, not a linguistic proof.

## How to measure later

```bash
uv run nas-subs models install --config config/config.yaml
uv run nas-subs benchmark /path/inside/a/root/clip.mkv --seconds 300 --json
```

Paste the JSON here. Do not invent an RTF or an ETA from it for titles that
were not in the measured window.
