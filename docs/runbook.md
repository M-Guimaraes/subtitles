# Runbook

Operational procedures. The install sequence has **not** been executed on a
NAS; it was written against the implementation and validated on an arm64 Mac.
Expect to adjust real paths on first contact with the server.

## 1. Survey the host

Record the answers in [benchmark.md](benchmark.md) before changing anything.

```bash
docker version && docker compose version
id -u && id -g
uname -m
nproc
free -g                                  # or the cgroup limit inside a VM
df -h /path/to/ssd
mountpoint -q /nas/media && echo mounted || echo NOT MOUNTED
ls /nas/media
```

Do not import, export or format ZFS. Do not change the pool, the USB
enclosure or any existing container.

## 2. Configure

```bash
cd ~/nas-subtitles
cp config/config.example.yaml config/config.yaml
cp .env.example .env
```

In `config/config.yaml` set `media_roots` to the real paths **as seen inside
the container** (the library is mounted at `/media`). In `.env` set
`APP_UID`/`APP_GID` from `id -u`/`id -g` and `MEDIA_HOST_PATH` to the host
library path.

```bash
mkdir -p data/state data/work data/models data/output
```

Put `data/` on the local SSD, never on the USB media disk and never on
SMB/NFS: SQLite needs local storage. Grant permissions on those four
directories only. No `chmod -R 777`, no global `chown`, no running as root.

## 3. Build and check the expansion

```bash
docker compose build
docker compose config          # confirm every bind path is what you expect
```

A missing media path fails the bind rather than being created, because
`create_host_path: false`. That is deliberate: a silent empty mount would
make the scanner think the library is empty.

## 4. Bootstrap the models (the only networked step)

```bash
docker compose run --rm -e HF_HUB_OFFLINE=0 subtitles \
    nas-subs models install --config /config/config.yaml
```

Then confirm they load with no network at all:

```bash
docker compose -f compose.yaml -f compose.offline.yaml run --rm subtitles \
    nas-subs models verify --offline --config /config/config.yaml
```

If the direct `en -> pt` package is unavailable, the bootstrap fails with an
instruction. Do not work around it with a pivot language or a remote API.

## 5. Diagnose, then preview

```bash
docker compose -f compose.yaml -f compose.offline.yaml run --rm subtitles \
    nas-subs doctor --config /config/config.yaml

docker compose -f compose.yaml -f compose.offline.yaml run --rm subtitles \
    nas-subs inspect /media/library/series/EPISODE.mkv --config /config/config.yaml

docker compose -f compose.yaml -f compose.offline.yaml run --rm subtitles \
    nas-subs process /media/library/series/EPISODE.mkv \
    --preview-seconds 300 --config /config/config.yaml
```

Only the video path changes in those commands. The preview lands in
`data/output` with `.preview` in the name; it never becomes a sidecar and
never satisfies the library.

## 6. One full episode in staging

Run the episode, then read the start, the middle, the end and **two chunk
boundaries** (around 300 s and 600 s). Check that no sentence is duplicated
or cut at a boundary, and that timing has not drifted.

```bash
docker compose -f compose.yaml -f compose.offline.yaml run --rm subtitles \
    nas-subs process /media/library/series/EPISODE.mkv --config /config/config.yaml
docker compose -f compose.yaml -f compose.offline.yaml run --rm subtitles \
    nas-subs jobs list --config /config/config.yaml --json
```

## 7. Scanner dry run

```bash
docker compose -f compose.yaml -f compose.offline.yaml run --rm subtitles \
    nas-subs scan --once --dry-run --config /config/config.yaml --json
```

Check the count, the exclusions and the paths. Do not release hundreds of
jobs before the pilot has been reviewed.

## 8. Pilot, then sidecar

Review at least 20 cues spread across the episode, including a joke, an
idiom, a proper name and a scene with music. Record what you find in
[benchmark.md](benchmark.md). The project goal is 18 of 20 cues keeping the
main meaning with no severe language error, and perceptible desync under
roughly 1 s in 18 of 20. This is a manual sample, not proof of overall
quality.

Only then, and only if you want files beside your videos, set
`publish_mode: sidecar` in `config.yaml` and add the override:

```bash
docker compose -f compose.yaml -f compose.sidecar.yaml run --rm subtitles \
    nas-subs publish JOB_ID --config /config/config.yaml
```

Both conditions are required: the config value **and** the override. Either
one alone will not write beside the media.

## 9. Local validation of automatic processing

Do this on the development machine before any NAS deploy. Use a disposable
copy of media, not the only copy of a title.

1. Point Compose at a local library (example):

   ```bash
   MEDIA_HOST_PATH=/Users/marceloguimaraes/Documents/Subtitles
   ```

   The container still sees that tree as `/media`. In `config/config.yaml` set
   `media_roots` to the matching **container** paths (for a single bind that is
   `/media`). Do not wipe `data/state/jobs.sqlite3` unless you intend to drop
   the queue.

2. For a short local wait, temporarily set `scan_interval_seconds`,
   `stability_window_seconds` and `minimum_file_age_seconds` to values you are
   willing to wait (for example 5 / 5 / 0). Restore the documented defaults
   afterwards.

3. Keep `publish_mode: staging` until a preview looks acceptable, then set
   `publish_mode: sidecar` and start the daemon with a writable media mount:

   ```bash
   docker compose -f compose.yaml -f compose.offline.yaml -f compose.sidecar.yaml up -d
   ```

   Equivalent without Docker (from a venv with models already installed):

   ```bash
   uv run nas-subs daemon --config /absolute/path/to/config/config.yaml
   ```

4. Copy a completed supported video into the configured library with **no**
   `*.pt-BR.srt` beside it. The first reconciliation should log `media
   discovered` / `media waiting for stability`. After size and mtime stay
   unchanged for `stability_window_seconds`, the file is queued, the existing
   pipeline runs, and `<stem>.pt-BR.srt` appears next to the video.

5. Restart the daemon (`docker compose restart subtitles` or interrupt and
   start `nas-subs daemon` again). The same file must not get a second job or
   a second sidecar.

6. Leave the sidecar in place and wait for another scan. Generation must be
   skipped (`existing target subtitle found` / `media skipped`). The existing
   SRT bytes must not change.

7. A file that fails processing must not stop later files in the same queue.

The media bind is read-only unless `compose.sidecar.yaml` is included. Sidecar
mode also needs write permission on the directories that will receive
subtitles; the process still never modifies the video file itself.

## 10. Run continuously

```bash
docker compose -f compose.yaml -f compose.offline.yaml up -d
docker compose logs -f subtitles
```

The optional dashboard is a second process. Add `compose.dashboard.yaml` and
open `http://127.0.0.1:8787`. Stopping that container does not stop the
worker. The host publish is loopback-only; set `NAS_SUBS_DASHBOARD_TOKEN`
before exposing the port on a LAN.

```bash
docker compose -f compose.yaml -f compose.offline.yaml -f compose.dashboard.yaml up -d
uv run nas-subs dashboard --config /absolute/path/to/config/config.yaml
```

For zero-touch sidecar publishing, add `compose.sidecar.yaml` and set
`publish_mode: sidecar` as in section 9.

Test a restart in the middle of a job and confirm it resumes without redoing
valid chunks. To start on boot, add one line to your existing `nas-start`
script **after** the pool is imported and verified:

```bash
/home/USER/nas-subtitles/scripts/nas-subtitles-start
```

That script refuses to start when the media path is not a mount point. It
does not modify or recreate any existing NAS script.

## Living with Bazarr and Jellyfin

**Choose one producer per title.** Bazarr may index the SRT this tool writes
and later replace it with a downloaded one. After the pilot, remove the
Bazarr profile from the titles handled here, or disable upgrades for that
scope, using whatever options your Bazarr version offers. This project never
changes Bazarr automatically and needs no API key.

In Jellyfin, first confirm the SRT exists and parses (read the file,
`ffprobe` it). Only if it still does not appear, refresh the library from the
dashboard. No Jellyfin API key is required.

## Maintenance

```bash
nas-subs health --config /config/config.yaml           # heartbeat + database
nas-subs cleanup --config /config/config.yaml --json   # dry run by default
nas-subs cleanup --apply --older-than-days 30 --config /config/config.yaml
nas-subs backup --destination /state/backups --config /config/config.yaml
```

`cleanup` is a dry run unless you pass `--apply`, and it only ever deletes
inside `work_dir` for a validated job ID. It never touches media. A
successful job loses its WAV files immediately; transcripts, translations and
manifests are kept for 30 days. Failed and under-review jobs keep their
checkpoints until you retry, cancel or clean them explicitly.

Backups use `sqlite3.Connection.backup`. Never copy `jobs.sqlite3` on its own
while WAL is active.

## Recovery

| Situation | What happens, and what to do |
|---|---|
| Another worker holds the lock | Exit code 6. Only one worker per `state_dir`; find the other process. |
| USB disk disappears | The job stops at its next I/O, checkpoints survive. Do **not** recreate the media directory on the system disk, or the scanner will index an empty tree. |
| Video changed | The fingerprint no longer matches, the old job and its artifacts are invalidated, and a new job is created for the new fingerprint. |
| Target name already taken | `output_conflict`, exit code 5. Both files are preserved; decide by hand. |
| Job stuck in `needs_review` | Intended. Inspect with `jobs show`, then `jobs approve` or `jobs cancel`. The worker never reopens it. |
| Repeated failure | 3 retries at 300/1800/7200 s, then `failed`. Permission, missing model, invalid media and conflicts do not retry at all. |
| Rolling back a release | Stop the container and redeploy the previous image tag. Never delete subtitles or the database to "reset". |
| Filesystem without hard links | `unsupported_atomic_publish`. Keep using staging; safe publication is not possible there. |

A read-only media root is perfectly valid in staging mode.
