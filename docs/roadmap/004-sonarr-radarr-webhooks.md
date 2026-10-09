# 004 — Sonarr/Radarr Webhook Triggers

**Status:** DONE

## Design choice

Webhooks are a dedicated `nas-subs webhooks` process and `compose.webhooks.yaml`
service, not routes on the dashboard. Stopping the listener does not stop the
worker or the dashboard. Import events call `discovery.enqueue_path` (stability
is not re-applied; Sonarr/Radarr already finished the import). A shared secret
is required. Bind defaults to loopback. Periodic scan remains the fallback.

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

Met: a valid Sonarr or Radarr `Download`/`Import` event for a file inside a
configured root is queued through `enqueue_path`; a bad token is 401; a
malformed payload or a path outside `media_roots` fails closed; a second
webhook for the same fingerprint returns the existing job; a later scan still
enqueues files the webhook never saw.
