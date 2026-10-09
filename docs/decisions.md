# Decisions

Why things are the way they are, including the ones that are uncomfortable.

## Stack

**Python 3.11, Debian slim.** Conservative base for native libraries
(CTranslate2, PyAV, sentencepiece). The patch version and base digest are
pinned once a build has been validated.

**uv with a committed `uv.lock`.** Reproducible resolution and `uv sync
--frozen` in the image, so the container installs exactly what was tested.

**faster-whisper / CTranslate2, Whisper `small`, `int8` on CPU.** Local
inference with word timestamps and VAD. `small` is the starting point for
comparison, not a proven optimum. Explicitly **not** `small.en`: the library
is multilingual, and the configuration rejects any `.en` model.

**Argos Translate, direct `en -> pt`.** Local translation with no service
behind it. `allow_pivot` is forced to `false`: routing through a third
language silently degrades quality. A public LibreTranslate instance is not
an option, because the whole point is to stop depending on quotas.

**Typer, Pydantic + PyYAML (`yaml.safe_load`), stdlib `sqlite3`, `srt`.**
Small dependency surface. The queue is a single local file, which is the
right size for one worker.

## Dependency resolution

`faster-whisper` and `argostranslate` both depend on CTranslate2 and were the
main compatibility risk. They resolved onto a single shared CTranslate2 build
with no conflict, and both import in the same environment. Exact versions are
in [benchmark.md](benchmark.md) and `uv.lock`. `--no-deps` is never used to
paper over a conflict.

`argostranslate` pulls in `stanza`, which pulls in `torch`. That is a large
transitive dependency for sentence segmentation and it dominates the image
size. It was accepted rather than worked around, because the alternative is
either hand-rolling sentence splitting or switching translation engines, and
switching engines is out of scope for this delivery. Worth revisiting if
image size becomes a real constraint.

**uv is installed from PyPI in the image**, not copied from the
`ghcr.io/astral-sh/uv` image. The ghcr.io pull hung during the build on this
machine, and depending on one registry (Docker Hub) plus PyPI is simpler than
depending on three.

## Safety

**`staging` is the default publish mode.** Nothing is written next to a video
until a human has read the output. Sidecar needs two independent opt-ins: the
config value and the Compose override.

**Hard link, not rename, for publication.** `os.link` fails with `EEXIST`
instead of destroying a file that something else created. A `rename` would
silently overwrite, which is unacceptable for a library the user cares about.
When the filesystem cannot do this, the job fails with
`unsupported_atomic_publish` rather than falling back to something unsafe.

**Sampled fingerprint, not a full hash.** 1 MiB from each end plus size and
`mtime_ns`. Hashing entire films on every scan would be absurdly expensive.
This is explicitly **not** cryptographic proof of equality; it detects the
usual changes (re-encode, resumed download, replacement). Known limitation:
renaming a file produces a new job.

**Two stable observations before queueing.** Size and `mtime_ns` must match
across a stability window, so a file still being copied is never processed.

**`needs_review` is a dead end for the worker.** Only a human command moves a
job out of it. Auto-reopening would eventually publish something nobody
looked at.

## Quality posture

**Gates are structural, not linguistic.** The checks verify indices, ordering,
overlap, bounds, width and reading speed. They cannot and do not judge
fluency. Calling them a quality guarantee would be a lie.

**Width and reading speed are targets, never a licence to delete text.**
1-7 s and 20 characters/second are goals. A violation produces a flag and a
review, not truncation. Text integrity and timeline correctness come first.

**Flags never erase words.** Repetition, low confidence or text in silence
are recorded in the manifest with the raw scores and the thresholds used.
Dropping words on a single score, with no measurement to justify the
threshold, would trade a visible problem for an invisible one.

**`pt-BR` is a filename, not a claim.** The model translates `en -> pt`. No
glossary, no blind find-and-replace, no invented Brazilian adaptation. If
Argos turns out to be too weak in the pilot, the honest response is to record
the limitation and a future comparison against OPUS-MT/Marian, not to swap
engines mid-delivery without updating lock, config and benchmark.

**Stream metadata is a candidate, not the truth.** Missing and `und` tags are
common. With `languages.source: auto`, Whisper samples are always taken and
combined with the selected stream tag using a documented policy in
`language.py`. Metadata alone is never a confident decision. Low confidence
becomes `needs_review` (`language_undetermined`); it does not silently assume
English. `pt` and `pt-BR` skip translation as the same public family; Argos
`pb` is never compared as if it were a public identifier.

## Configuration

**Everything is validated up front with clear messages,** and unknown keys
are rejected so a typo cannot silently fall back to a default.

**Internal paths must be absolute.** Relative paths are refused rather than
resolved against an ambiguous working directory.

**Working directories may not live inside a media root.** The application
must never write into the library, so this is a schema error rather than a
runtime surprise.

**Two layers of config hashing.** `pipeline_config_hash` covers the settings
that change what the pipeline produces, and participates in job uniqueness.
`stage_config_hash(stage)` is per stage, so changing the subtitle width
invalidates rendering without discarding hours of valid transcription. Both
are deterministic SHA-256 over canonical JSON and carry a schema version, so
the hashing scheme itself can be changed deliberately.

**`worker.concurrency` is constrained to 1.** The MVP is explicitly one job
at a time; accepting a larger number in the schema would imply support that
does not exist.

**`stale_lease_seconds` must exceed `heartbeat_seconds`.** Otherwise a
perfectly healthy worker loses its own lease. Cheap to validate, confusing to
debug.

## Observability

**JSON logs on stdout with no speech and no personal paths.** Logs get
exported and pasted into issues. Call sites log a `root_id` and a
`path_token()` hash; the formatter also redacts a set of known-sensitive keys
as a backstop. Full detail stays in local artifacts.

**No Prometheus.** Netdata already covers container CPU and memory. Adding a
metrics stack for an MVP would be unjustified.

**ETA comes from measured jobs or not at all.** No invented constant, and no
promise about how long the whole library will take.

## Deliberately out of scope

OCR and PGS, diarization, subtitle providers, translating downloaded
subtitles, GPU, multiple workers, LLM translation, Redis, PostgreSQL,
Celery, Kubernetes. A LAN dashboard exists as a separate process and is not
required for processing. Sonarr/Radarr webhooks are another optional
process; they only enqueue through the existing discovery path and are not
required for processing. No changes to ZFS, the USB enclosure,
`~/nas-stack`, Bazarr, Jellyfin or any existing container.
