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

## Dubbing engines (roadmap 006 fase 2)

One-off measurement of `DemucsSeparator` (`htdemucs`) and `PiperSynthesizer`
(`pt_BR-faber-medium`) on the development Mac (Apple Silicon, 10 logical
cores, 16 GiB, CPU only). Input: 180 s of the first English-language audio
stream of one local episode (`ffmpeg`, 16 kHz mono), starting at 10:00. A
single run each, not a distribution.

| Engine | Input | Wall time | Peak RSS (process) |
|---|---|---|---|
| Demucs `htdemucs` separate | 180 s audio | 43.3 s (0.24x realtime) | 2675 MiB |
| Piper synthesize | 1 sentence, 3.9 s of speech | 1.2 s (includes first voice load) | 2675 MiB (unchanged; Demucs ran first) |

Objective checks only: the dialogue stem kept 16 kHz mono and the chunk
length; RMS of the original, dialogue and accompaniment was 0.0166, 0.0156
and 0.0053 on a dialogue-heavy scene. **No human listening review was done**,
so nothing here says how clean the separation is or how the Piper voice
sounds. Demucs is a music separator: effects and room tone may leak into
either stem. Accompaniment is 16 kHz mono because that is what `extract`
produces; a proper mix needs the original channels (fase 3).

Backend decision: Demucs `htdemucs` stays as the separation baseline. This
does not measure a full episode, other backends, or the NAS.
