# nas-subtitles

Generate Portuguese subtitles for your own media library from the audio
itself, entirely on your own hardware. No subtitle providers, no quotas, no
translation API.

A video goes in, a `.pt-BR.srt` comes out: the audio track is selected and
extracted with FFmpeg, transcribed locally with faster-whisper, translated
locally with Argos Translate, rendered as SRT and then published without ever
overwriting anything.

## What `pt-BR` does and does not mean

**The `pt-BR` suffix is the destination we want, not a guarantee of what the
translation produces.** The model translates `en -> pt`. It outputs
Portuguese, and that Portuguese may well use European vocabulary and
phrasing. Nothing in this project adapts the text to Brazilian Portuguese,
and no glossary or find-and-replace rules are applied.

More generally, this does not promise professional translation, absence of
hallucination, or perfect synchronisation. That is exactly why `staging` is
the default publication mode and why the review gates exist: you look at the
result before anything is written next to your videos.

## Status

The software is implemented and tested on an Apple Silicon (arm64) Mac.

**It has not been deployed to a NAS.** The server was not reachable while
this was built, so there is no measurement of it running there, no pilot on a
real episode, and no claim about any other CPU architecture. See
[docs/benchmark.md](docs/benchmark.md) for exactly what was measured and what
is still blocked.

## Requirements

Docker and Compose v2, or Python 3.11 with [uv](https://docs.astral.sh/uv/)
and FFmpeg for a local checkout. Roughly 4 GiB of RAM and 3 GiB of free disk
for intermediates.

## Quick start

```bash
cp config/config.example.yaml config/config.yaml   # then edit media_roots
cp .env.example .env                               # set APP_UID, APP_GID, MEDIA_HOST_PATH
mkdir -p data/state data/work data/models data/output

docker compose build
docker compose config          # check the paths expanded the way you expect

# The one step that needs network: download the models.
docker compose run --rm -e HF_HUB_OFFLINE=0 subtitles \
    nas-subs models install --config /config/config.yaml

# Everything after this runs with no network at all.
docker compose -f compose.yaml -f compose.offline.yaml run --rm subtitles \
    nas-subs models verify --offline --config /config/config.yaml
docker compose -f compose.yaml -f compose.offline.yaml run --rm subtitles \
    nas-subs doctor --config /config/config.yaml
```

Try a five-minute preview before trusting it with anything, then review the
SRT that lands in `data/output`:

```bash
docker compose -f compose.yaml -f compose.offline.yaml run --rm subtitles \
    nas-subs process /media/library/series/YOUR_EPISODE.mkv \
    --preview-seconds 300 --config /config/config.yaml
```

Start the worker only once you are satisfied:

```bash
docker compose -f compose.yaml -f compose.offline.yaml up -d
```

The full sequence, including the scanner dry run and the pilot, is in
[docs/runbook.md](docs/runbook.md).

## Commands

```text
nas-subs doctor              environment, paths, resources and publish support
nas-subs models install      download the Whisper model and the en->pt package
nas-subs models verify       confirm the models load offline
nas-subs inspect PATH        streams, duration and the audio track that wins
nas-subs process PATH        run one file ahead of the queue
nas-subs enqueue PATH        add one file to the queue
nas-subs scan                walk the roots and queue stable, unsubtitled videos
nas-subs worker              the daemon: periodic scan plus serial processing
nas-subs jobs list|show|retry|cancel|approve
nas-subs publish JOB_ID      publish an approved job without re-transcribing
nas-subs benchmark PATH      measure throughput and memory on a short window
nas-subs health              heartbeat and database check, used by Docker
nas-subs cleanup             remove stale intermediates inside work_dir
nas-subs backup              consistent backup of config, manifest and database
```

Every command accepts `--json` and none of them prompts. Exit codes: `0`
success, `2` bad arguments or configuration, `3` preflight failure, `4`
processing failure, `5` review required or conflict, `6` another worker holds
the lock.

## How it protects your library

- The media bind mount is **read-only** by default. Writing beside a video
  requires both `publish_mode: sidecar` and the `compose.sidecar.yaml`
  override.
- Videos are never remuxed, re-encoded, renamed or deleted.
- An existing file at the target name is **never** overwritten. That is an
  `output_conflict`, and both files are kept.
- Videos that already have a complete Portuguese subtitle, external or
  embedded, are skipped. A `forced` subtitle does not count as complete.
- Files are only queued once their size and mtime have been stable across two
  observations, so a file still being copied is never processed.

## Living with Bazarr and Jellyfin

Pick one producer per title. Bazarr will happily index the SRT this tool
creates and may later replace it, so after the pilot remove the Bazarr
profile from the titles handled here, or disable upgrades for that scope.
This project never changes Bazarr or Jellyfin, and needs no API key for
either. Details in [docs/runbook.md](docs/runbook.md).

## Configuration

Copy `config/config.example.yaml`, which documents every option and its
default. Paths in it are container paths and must be absolute. Notable
defaults: `publish_mode: staging`, Whisper `small` with `int8` on CPU, 300 s
chunks with 2 s overlap, and subtitles of up to 2 lines of 42 characters.

## Documentation

- [docs/architecture.md](docs/architecture.md) — how the pieces fit together
- [docs/runbook.md](docs/runbook.md) — install, pilot and recovery procedures
- [docs/benchmark.md](docs/benchmark.md) — what was measured, what is blocked
- [docs/decisions.md](docs/decisions.md) — why things are the way they are
- [AGENTS.md](AGENTS.md) — rules and module ownership for contributors
