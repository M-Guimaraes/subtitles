# **001 — Automatic Processing Service**

**Status:** DONE

## **Goal**

Make normal operation zero-touch: completed media imported into the configured library is automatically discovered, queued, processed, and published as a subtitle sidecar without manual Docker or CLI execution.

The final expected user experience is:

```text
Sonarr / Radarr imports media
        ↓
media appears in configured library
        ↓
nas-subtitles reconciliation scanner discovers it
        ↓
file stability is verified
        ↓
existing target subtitle is checked
        ↓
persistent job is queued
        ↓
existing worker/pipeline processes it
        ↓
subtitle is atomically published
        ↓
<media-stem>.pt-BR.srt
        ↓
Jellyfin discovers the subtitle
```

This roadmap item must reuse the existing repository, discovery concepts, job state machine, worker, transcription, translation, validation, and pipeline infrastructure.

Do not create a second processing path specifically for daemon operation.

---

# **User Story**

When Sonarr or Radarr finishes importing a media file into the library, I want `nas-subtitles` to process it automatically so Jellyfin receives the requested subtitle without manual Docker or CLI work.

---

# **Scope**

Implement a long-lived automatic processing service containing:

- periodic library reconciliation;
- stable-file detection;
- existing-subtitle detection;
- persistent queueing;
- persistent worker execution;
- restart/recovery behavior;
- production sidecar publishing;
- Docker Compose daemon deployment.

Normal operation after this feature must not require:

```bash
nas-subs process ...
```

The CLI must remain available for administration, debugging, previews, and manual processing.

---

# **Architecture**

The intended architecture is:

```text
Configured media roots
        ↓
Reconciliation scanner
        ↓
Stable-file verification
        ↓
Existing subtitle policy
        ↓
Existing discovery/repository
        ↓
Persistent job queue
        ↓
Existing worker
        ↓
Existing pipeline
        ↓
Atomic sidecar publisher
        ↓
Jellyfin
```

There must be only one processing pipeline.

Manual CLI processing and daemon processing must ultimately reuse the same underlying pipeline behavior.

---

# **1. Reconciliation Scanner**

Implement periodic reconciliation scanning of configured media roots.

For this roadmap item, use **periodic scanning only**.

Do NOT introduce:

- watchdog;
- inotify;
- filesystem event watchers;
- Sonarr webhooks;
- Radarr webhooks.

Sonarr/Radarr webhooks belong to roadmap item `004`.

The reconciliation scanner is the correctness mechanism.

It must recover files that appeared:

- while the service was stopped;
- while the container was restarting;
- while a previous scan was running;
- between scan intervals.

Default scan interval:

```yaml
scan_interval_seconds: 600
```

The value must be configurable.

Avoid busy polling.

---

# **2. Media Discovery**

Scan only configured media roots.

Example configuration direction:

```yaml
discovery:
  roots:
    - /media/library/series
    - /media/library/movies
```

Do not hardcode `/nas/media` or any deployment-specific host path inside application code.

Supported media extensions must be centrally defined or configurable.

Examples may include:

```text
.mkv
.mp4
.m4v
.avi
.mov
```

Use existing project conventions when deciding where this configuration belongs.

Unsupported files must be ignored.

Subtitle files themselves must never become media jobs.

---

# **3. Stable-File Protection**

Never process a media file that is still being downloaded, copied, moved, or imported.

Use a deterministic stability rule based on:

```text
size + mtime
```

A candidate becomes eligible only after both values remain unchanged for at least:

```yaml
stability_seconds: 60
```

The value must be configurable.

The scanner must therefore be capable of observing a candidate across reconciliation cycles.

A newly discovered file must not immediately be assumed stable merely because it currently exists.

Files whose size or mtime changes must restart their stability period.

Prefer final library paths.

---

# **4. Temporary / Partial Files**

Ignore known temporary or partial download/import files.

Examples include extensions or suffixes such as:

```text
.part
.partial
.tmp
.temp
.!qB
```

Follow existing conventions where appropriate and keep this logic centralized/testable.

A temporary file must never generate a processing job.

---

# **5. Existing Subtitle Policy**

Before automatically generating a subtitle, determine whether a valid sidecar already exists for the requested logical target language.

Default policy:

```yaml
existing_subtitle_policy: skip
```

For target language:

```text
pt-BR
```

a media file such as:

```text
episode.mkv
```

should consider the canonical generated sidecar:

```text
episode.pt-BR.srt
```

The application must use the **logical/public target language** in filenames.

Never expose backend-specific language codes.

For example, Argos internally uses:

```text
pb
```

for Brazilian Portuguese.

That must never produce:

```text
episode.pb.srt
```

The correct filename remains:

```text
episode.pt-BR.srt
```

If the target sidecar already exists:

- do not overwrite it automatically;
- do not enqueue unnecessary processing;
- record/log why the file was skipped.

The skip decision must be observable through appropriate application logging/state.

---

# **6. Queue and Idempotency**

Filesystem reconciliation will repeatedly encounter the same files.

Repeated scans must not create duplicate effective work.

Reuse the existing:

- media fingerprint;
- pipeline configuration identity;
- repository;
- job uniqueness behavior.

The system must remain idempotent when:

- the same file appears in multiple scans;
- the service restarts;
- scans overlap;
- the same candidate is rediscovered;
- processing previously completed successfully.

Do not create a second queue implementation.

---

# **7. Worker**

The daemon must consume persistent queued jobs using the existing worker/pipeline infrastructure.

The worker must continue processing unrelated jobs when one job fails.

Example:

```text
Job A → success
Job B → failure
Job C → success
```

Failure of Job B must not terminate:

- discovery;
- the daemon;
- processing of Job C.

Avoid tight retry loops.

Existing retry/state-machine semantics should be reused whenever possible.

---

# **8. Restart and Recovery**

Jobs must survive:

- application restart;
- container restart;
- NAS reboot.

Define deterministic recovery behavior for jobs that were interrupted while running.

A job left in a transient/running state because the process crashed must not remain permanently stuck.

Reuse or extend the existing state machine rather than bypassing it.

Recovery must not duplicate successfully completed work.

The service should be safe to start repeatedly.

---

# **9. Sidecar Publishing**

Add a production sidecar publishing mode.

Final filename:

```text
<media-stem>.<logical-target-language>.srt
```

Example:

```text
Dexter.S03E01.1080p.5.1Ch.BluRay.ReEnc-DeeJayAhmed.mkv
```

produces:

```text
Dexter.S03E01.1080p.5.1Ch.BluRay.ReEnc-DeeJayAhmed.pt-BR.srt
```

The subtitle must be written next to the media file.

Do not modify, rename, move, truncate, or delete the source media file.

---

# **10. Atomic Publication**

Sidecar publication must be atomic.

Jellyfin must never observe a partially written final subtitle.

Do not stream output directly into:

```text
episode.pt-BR.srt
```

Instead:

1. generate/write a temporary file in the destination directory;
2. flush/close it successfully;
3. atomically rename/replace it into the final path.

Conceptually:

```text
.episode.pt-BR.srt.tmp
        ↓
complete write
        ↓
atomic rename
        ↓
episode.pt-BR.srt
```

Use an appropriate same-filesystem atomic operation such as `os.replace()`.

Respect `existing_subtitle_policy`.

The default `skip` policy must not silently overwrite a sidecar that appeared between discovery and publication.

Handle that race safely.

---

# **11. Preserve Existing CLI / Preview Behavior**

Current CLI behavior must continue working.

In particular, preserve existing:

- preview processing;
- staging output;
- manual processing;
- model management;
- job administration.

Production sidecar publishing must be an explicit publishing mode and must not accidentally change preview behavior.

Current preview output such as:

```text
*.pt-BR.preview.srt
```

must remain isolated from production sidecar publishing.

---

# **12. Configuration**

Follow existing project configuration conventions rather than creating an unrelated configuration system.

The desired configuration direction is conceptually:

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

These names are illustrative.

Prefer compatibility with existing configuration models and naming.

Configuration must be validated.

Invalid values such as:

```text
scan_interval_seconds <= 0
stability_seconds < 0
```

must be rejected appropriately.

---

# **13. Daemon Command**

Provide a clear CLI entry point for running the long-lived service.

For example:

```bash
nas-subs daemon --config /config/config.yaml
```

or an equivalent command consistent with the existing CLI architecture.

The command must:

1. initialize persistence;
2. recover interrupted jobs when applicable;
3. start reconciliation;
4. process queued jobs;
5. continue scanning at the configured interval;
6. survive individual job failures;
7. stop cleanly on SIGTERM/SIGINT.

Container shutdown must not require SIGKILL during normal operation.

---

# **14. Docker Deployment**

Provide/update Docker Compose configuration suitable for the Ubuntu NAS.

The service should run continuously using:

```yaml
restart: unless-stopped
```

Host deployment currently uses media under:

```text
/nas/media
```

but this path must remain a deployment concern.

Application code must see configured container paths such as:

```text
/media/library/series
/media/library/movies
```

or equivalent configured paths.

The deployment must make the media library writable where sidecar publication requires it.

Do not give the application unnecessary behavior that modifies source media.

Document the permission requirements.

The daemon must continue using the existing persistent volumes/directories for:

- state/database;
- work files;
- models;
- configuration.

---

# **15. Permissions**

Production sidecar publishing requires write access to the media directory.

Current read-only media mounts used during development/preview must not simply be changed without documenting the consequence.

The production Compose example must explicitly support sidecar creation.

The application must only intentionally write:

- its own state/work directories;
- generated subtitle sidecars;
- temporary files required for atomic subtitle publication.

It must never intentionally modify the media file itself.

---

# **16. Logging / Observability**

Automatic processing must provide enough structured logging to understand decisions.

Important events should include:

```text
media discovered
media waiting for stability
media became stable
unsupported media ignored
temporary file ignored
existing target subtitle found
media skipped
job queued
job started
job completed
job failed
job recovered after restart
sidecar published
```

Do not spam logs every few seconds for unchanged files.

Normal reconciliation should remain readable.

---

# **17. Failure Isolation**

A malformed or problematic media file must not stop automatic processing globally.

Example:

```text
movie-a.mkv → processing failure
movie-b.mkv → still processed normally
```

Discovery must continue even when worker jobs fail.

Publishing failure must leave the final sidecar either:

- completely valid; or
- absent.

Never leave a partially generated final sidecar.

Temporary files should be cleaned up when safely possible.

---

# **18. Tests**

Add automated tests covering at minimum:

### **Discovery**

- initial scan discovers eligible media;
- newly added media is discovered;
- unsupported files are ignored;
- subtitle files are ignored;
- temporary/partial files are ignored.

### **Stability**

- newly observed file is not immediately processed;
- unchanged size + mtime becomes eligible after `stability_seconds`;
- size change resets stability;
- mtime change resets stability;
- unstable file is not queued.

### **Existing subtitles**

- existing target sidecar causes skip;
- existing sidecar is not overwritten;
- skip reason is observable;
- `pt-BR` filename is recognized correctly;
- backend code `pb` is never used as sidecar language.

### **Idempotency**

- repeated scan does not create duplicate effective work;
- completed work is not unnecessarily repeated;
- service restart does not duplicate completed jobs.

### **Queue / Worker**

- eligible media is queued;
- queued job uses existing worker/pipeline;
- one failed job does not stop worker;
- later queued job executes after another job fails.

### **Recovery**

- interrupted job has deterministic recovery;
- restart does not leave recoverable jobs permanently running;
- completed jobs remain completed.

### **Publishing**

- final sidecar follows expected naming;
- publication is atomic;
- temporary output is not mistaken for final subtitle;
- failed publication does not expose partial final file;
- source media remains untouched;
- sidecar appearing during processing is not overwritten under default `skip` policy.

### **CLI / Regression**

- daemon command starts correctly;
- existing CLI commands remain compatible;
- preview/staging behavior remains unchanged.

---

# **19. Acceptance Criteria**

This roadmap item is complete when the following scenario works:

Given:

```text
a completed supported media file exists inside a configured library
```

and:

```text
no target-language sidecar exists
```

and:

```text
nas-subtitles daemon is running
```

then:

1. reconciliation discovers the media;
2. the application verifies file stability;
3. exactly one effective job is created;
4. the existing pipeline processes the media;
5. the subtitle is generated;
6. it is atomically published beside the media;
7. the final filename uses the logical target language;
8. Jellyfin can discover the sidecar;
9. no manual `nas-subs process` command is required.

For example:

```text
Dexter.S03E01.mkv
Dexter.S03E01.pt-BR.srt
```

Restarting the daemon must not duplicate successfully completed work.

An existing target subtitle must not be overwritten by default.

A failed media job must not prevent other media from processing.

---

# **20. Local Validation Before NAS Deployment**

Do not deploy directly to the NAS after implementation.

First provide a controlled local validation procedure.

The local validation should demonstrate:

```text
daemon starts
    ↓
test media appears in configured test library
    ↓
first observation marks it unstable
    ↓
stability period passes
    ↓
media is queued
    ↓
pipeline runs
    ↓
sidecar appears automatically
```

Then validate:

```text
daemon restart
    ↓
same media scanned again
    ↓
no duplicate processing
```

Then validate:

```text
existing .pt-BR.srt
    ↓
media scanned
    ↓
generation skipped
```

Only after these tests succeed should the feature be deployed to the Ubuntu NAS.

---

# **21. Out of Scope**

Do NOT implement as part of roadmap item 001:

- Sonarr webhook;
- Radarr webhook;
- web dashboard;
- filesystem watcher/inotify/watchdog;
- multiple target languages;
- unrelated ASR changes;
- translation model changes;
- Whisper configuration changes;
- subtitle quality heuristics unrelated to automatic processing;
- unrelated refactors.

These belong to later roadmap items.

---

# **Implementation Constraints**

1. Reuse the existing architecture wherever possible.
2. Do not create a second processing pipeline.
3. Do not weaken or remove existing tests to make implementation pass.
4. Preserve existing CLI behavior.
5. Preserve current preview behavior.
6. Preserve current PT-BR/Argos `pb` abstraction.
7. Do not modify source media.
8. Prefer small focused components over a monolithic daemon loop.
9. Keep filesystem, clock/time, repository, and worker behavior testable.
10. Avoid sleeping for real time in unit tests; inject/fake clock behavior where appropriate.
11. Database/schema migrations must be explicit if persistence changes are required.
12. Existing jobs/databases should remain compatible whenever reasonably possible.

---

# **Required Agent Report**

After implementation, do not immediately proceed to another roadmap item.

Report back with:

1. files created;
2. files modified;
3. architecture/components introduced;
4. configuration changes;
5. CLI changes;
6. database migrations, if any;
7. restart/recovery behavior implemented;
8. sidecar publication strategy;
9. tests added;
10. result of the complete test suite;
11. exact command for local daemon startup;
12. exact local end-to-end validation procedure;
13. any assumptions or limitations discovered during implementation.

Also update `ROADMAP.md` and the roadmap item status only after implementation and tests are complete.

Do not implement roadmap item 002 or later items as part of this task.