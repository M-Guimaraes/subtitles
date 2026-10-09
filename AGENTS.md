# AGENTS.md

Instructions for any agent or developer working in this repository.

## What this project is

A CLI and single worker that generate Portuguese subtitles from the audio of
a locally stored video, using local models only. No subtitle providers, no
translation APIs, no telemetry.

## Development commands

```bash
uv sync --frozen --group dev        # install exactly what uv.lock pins
uv run nas-subs --help              # the CLI
uv run ruff check .                 # lint
uv run ruff format .                # format (CI runs --check)
uv run mypy src                     # strict typing on src/
uv run pytest -m 'not models'       # the suite that runs without models
uv run pytest -m models             # smoke tests; needs installed models
docker compose config               # validate the Compose expansion
docker compose build                # build the runtime image
```

Add a dependency with `uv add <pkg>` (or `uv add --group dev <pkg>`) and
commit the updated `uv.lock`. Never hand-write a version into
`pyproject.toml` to dodge a resolution conflict, and never use `--no-deps` to
hide one: `faster-whisper` and `argostranslate` must keep sharing one
`ctranslate2` build, and both must import in the same environment.

## Hard rules

**Offline inference is a requirement, not a preference.** The only command
allowed to touch the network is `nas-subs models install`. Nothing may
download at import time or at worker startup, and no code path may fall back
to a remote API when a local model is missing. Missing model means a clear
`model_missing` failure.

**Never modify the user's media.** The library is mounted read-only by
default. The application does not remux, re-encode, rename or delete video,
and it does not write metadata into video containers. The only thing it ever
creates next to a video is a new `.pt-BR.srt`, and only in `sidecar` mode.

**Never overwrite an existing file.** Publication creates a uniquely named
temporary file in the target directory and then hard-links it to the final
name. `EEXIST` is an `output_conflict`, never an overwrite. No `rename` onto
a final path, no partial writes to a final path.

**Safe deletion only.** Cleanup touches paths inside `work_dir` whose job ID
has been validated. Never delete by glob, never outside `work_dir`.

**Subprocesses take argv lists.** Always `shell=False` with a timeout.
Filenames contain spaces, Unicode and shell metacharacters; they must never
be interpreted.

**Logs are exportable.** No transcribed speech and no personal paths in log
payloads. Use `path_token()` and a `root_id`.

**No claims that were not measured.** Do not state that the image works on an
architecture that was not built and smoke-tested, do not claim NAS
deployment, and do not describe translation quality that nobody reviewed.
`en -> pt` produces Portuguese; it does not guarantee Brazilian Portuguese.

## Module ownership by stage

Create files only in the modules your stage owns. Everything in the table
already exists as a typed stub, so no stage needs to create a new file and
parallel work does not collide. Changing a shared contract means changing
`domain.py`, which affects everyone: do it deliberately and say so.

| Stage | Owns | Must not edit |
|---|---|---|
| 1. foundation *(done)* | `domain.py`, `config.py`, `logging_setup.py`, `cli.py`, `health.py`, `pyproject.toml`, `uv.lock`, `Dockerfile`, `compose*.yaml`, `.env.example`, `scripts/`, `AGENTS.md`, `README.md` | — |
| 2. state | `repository.py`, `states.py`, `migrations/001_initial.sql` | `domain.py` contracts, engine modules |
| 3. media | `discovery.py`, `media.py`, `language.py` | `repository.py`, `output.py` |
| 4. asr | `transcription.py`, `models.py` (ASR half) | `translation.py`, `output.py` |
| 5. translation | `translation.py`, `models.py` (Argos half) | `transcription.py`, `output.py` |
| 6. output | `segmentation.py`, `quality.py`, `output.py` | `repository.py`, engine modules |
| 7. worker | `worker.py`, `pipeline.py` | engine internals |
| 8. delivery | `docs/`, Compose overrides, `doctor` checks in `cli.py` | everything else |
| 003. dashboard | `api.py`, `dashboard.py`, `static/` | worker/pipeline internals |
| 004. webhooks | `webhooks.py`, `compose.webhooks.yaml` | worker/pipeline internals |
| 006. dubbing | `dubbing.py`, `migrations/004_job_kind.sql`, dubbing half of `models.py`/`cli.py` | subtitle engine internals |

Shared, owned by stage 1, consumed by all: `domain.py` (protocols, enums,
value objects, `ExitCode`) and `config.py` (`AppConfig`,
`pipeline_config_hash`, `stage_config_hash`).

Stubs raise `NotImplementedError` with a message naming the owning stage. The
CLI turns that into a structured `not_implemented` failure with exit code 4,
so an unfinished command never prints a traceback.

## Conventions

- Timestamps in seconds are always absolute on the **video** timeline, never
  relative to a chunk or to a stream's `start_time`.
- Chunk `k` owns `[k * chunk_seconds, min((k+1) * chunk_seconds, duration))`.
  Overlap is context for the decoder, never a source of duplicate words.
- A global ffprobe stream index is never an `a:N` ordinal.
- Exit codes come from `ExitCode` only: 0 success, 2 bad arguments or config,
  3 preflight, 4 processing failure, 5 review or conflict, 6 lock held.
- Every CLI command takes `--json` and never prompts.
- Checkpoints are written to a temporary file, flushed, then renamed on the
  same filesystem, and are only reused when job, fingerprint, schema version,
  stage config hash and model identity all match.

## Out of scope for the MVP

OCR and PGS, diarization, subtitle providers, translating downloaded
subtitles, a web UI or API, authentication, GPU, multiple concurrent workers,
LLM translation, Redis, PostgreSQL, Celery and Kubernetes. Also out of scope:
changing ZFS, the USB enclosure, `~/nas-stack`, Bazarr, Jellyfin or any
existing container.

## Testing notes

Fixtures must be synthetic (generate them with FFmpeg in the test) or
permissively licensed with the origin recorded. No personal media in the
repository. Integration tests use fake engines so results are deterministic;
never assert an exact sentence produced by a model. Tests needing real models
carry the `models` marker.
