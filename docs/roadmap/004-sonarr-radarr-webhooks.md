# 004 — Sonarr/Radarr Webhook Triggers

**Status:** BACKLOG

## Goal

Use Sonarr/Radarr import/download-complete events as a fast trigger while retaining periodic reconciliation as the correctness fallback.

## Requirements

- Accept supported Sonarr/Radarr webhook events.
- Resolve the final imported media path safely.
- Feed the same discovery/enqueue path used by filesystem/reconciliation processing.
- Duplicate webhook + filesystem events must remain idempotent.
- Validate/authenticate webhook requests using an appropriate local secret/API mechanism.
- Do not make Sonarr/Radarr availability mandatory for nas-subtitles operation.

## Acceptance

A completed import can trigger processing immediately, while a missed webhook is eventually recovered by the normal reconciliation scan.
