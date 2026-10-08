# 001 — Automatic Processing Service

**Status:** PLANNED

## Goal

Make normal operation zero-touch: imported media is automatically queued, processed, and published as a subtitle sidecar.

## User story

When Sonarr or Radarr finishes importing a media file into the library, I want nas-subtitles to process it automatically so Jellyfin receives the requested subtitle without manual Docker/CLI work.

## Scope

Implement a long-lived service containing discovery/reconciliation + persistent worker behavior. Reuse the existing repository/jobs/pipeline instead of creating a second processing path.

### Discovery

Monitor configured media roots and perform periodic reconciliation scans. The reconciliation scan is the correctness mechanism and must recover files missed while the service was stopped.

Supported media extensions should be configurable or centrally defined.

### Stable-file protection

Never process an actively downloading/copying file. Establish a deterministic stability/import rule. Prefer final library paths and stable size/mtime checks. Temporary/partial extensions must be ignored.

### Existing subtitle policy

Default policy: if a valid sidecar for the requested target language already exists, skip automatic generation. Record why it was skipped. Do not overwrite existing subtitles by default.

### Queue/idempotency

Duplicate filesystem events/scans must not create duplicate effective work. Reuse fingerprints and pipeline identity.

### Worker

Worker consumes queued jobs and runs the existing pipeline. Jobs and recovery survive container/service restart.

### Publishing

Add production sidecar publishing. Final filename follows `<media-stem>.<logical-target-language>.srt`, e.g. `episode.pt-BR.srt`.

Publish atomically. A partially generated subtitle must never appear under the final filename.

### Deployment

Provide Docker Compose production example for Ubuntu NAS. Service starts automatically (`restart: unless-stopped`). Media mapping should reflect `/nas/media` deployment while remaining configurable.

CLI must continue working.

## Suggested configuration direction

```yaml
service:
  mode: daemon
  scan_interval_seconds: 600
  stability_seconds: 60

discovery:
  roots:
    - /media/library/series
    - /media/library/movies

publishing:
  mode: sidecar
  existing_subtitle_policy: skip
```

Names are illustrative; follow existing config conventions.

## Failure/restart behavior

A crash must not lose jobs. Interrupted jobs must have a defined recovery/retry state. A failure processing one file must not stop discovery or unrelated jobs.

## Tests

At minimum: initial scan enqueue; repeated scan idempotency; new-file detection; partial/unstable file ignored; existing target subtitle skipped; atomic publication; restart recovery; one failed job does not halt worker; unsupported files ignored.

## Acceptance

Given a completed media file in a configured library and no target subtitle, the running service automatically produces the sidecar without a manual `process` command. Restarting the service does not duplicate completed work. Existing target subtitles are not overwritten by default.

## Out of scope

Sonarr/Radarr webhooks and web dashboard. Those are later roadmap items.
