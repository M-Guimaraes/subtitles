# 003 — Web Dashboard

**Status:** IN PROGRESS

## Goal

Provide visibility and operational control without making the web UI a dependency of the processing pipeline.

## Initial screens

### Queue / jobs

Show media title/path, state, progress/stage, detected source language/confidence, target language, timestamps, and error summary.

### Job detail

Show pipeline stages, selected audio stream, model identities, output path, diagnostics, and available actions.

### History

Completed, skipped and failed jobs with filtering.

### Settings

Expose safe operational settings such as automatic processing, roots, target language, source auto/override, scan interval, existing-subtitle policy and processing model where supported.

## Actions

Retry failed job, cancel supported job, request manual reprocess, and refresh/rescan library. Actions must use application/service APIs rather than duplicating pipeline logic in the UI.

## Architecture

Prefer a small API layer over the existing repository/service abstractions. The worker continues functioning if the dashboard/API is unavailable.

## Security

Initial deployment is LAN/private-network oriented. Do not expose an unauthenticated administrative dashboard to the public internet. Design the API so authentication can be added cleanly before remote/public exposure.

## Acceptance

User can open the dashboard and determine what is queued, processing, completed, skipped or failed; inspect why a job failed/skipped; and retry a failed job. Stopping the dashboard must not stop background processing.
