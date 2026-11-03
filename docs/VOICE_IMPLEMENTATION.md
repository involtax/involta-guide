# Voice implementation report

Specification: `docs/VOICE_SERVICE_SPEC.md` from `docs/voice-service-spec`, commit
`90c396f16b068962088998e9eebe719cd82416b5`, copied without editing. First contract
commit: `8198310`. Core implementation: `934347d`; subsequent commits integrate
current main and complete validation. No production deployment or merge.

## Checked stages

1. Contract: explicit versioned DTOs, OpenAPI/JSON Schema, ownership and error
   semantics. Both Python packages ship in the wheel/image. AST regression test
   forbids Guide imports inside Voice. Compose supplies a separate DB cluster,
   credentials and persistent audio volume.
2. Intake/worker: atomic audio persistence, idempotency and conflict handling,
   bounded intake, duration/MIME verification, transcription checkpoint, raw
   response retention, immutable run snapshots, lease fencing and explicit
   unknown paid attempts. Fake provider tests include 403/429/500, expired lease,
   saved-response recovery, budget guard and disk failure. Analysis failure
   preserves original text/audio. No paid OpenAI calls were used.
3. Integration: HTTP-only service boundary, outbox/inbox with acknowledgement
   after commit, replay/out-of-order handling and independent runs. Tests cover
   lost acknowledgement and concurrent replay without duplicate reviews. Guide
   keeps its own review/moderation journal. Browser ownership and forged actor
   checks are exercised; new and legacy history remain readable.
4. Editor: original/editable text, optional binding and fragments, independent
   moderation/text visibility/proposal decisions, model and prompt controls,
   run logs and revalidation. Tests cover stale revisions, repeated decisions,
   partial acceptance and explicit page publication with atomic decision audit.
   The administrator-cookie path preserves Guide's trusted command executor.
5. Rollout: routing defaults off, additive migration follows main's
   `0005_current` as `0006_voice`, archives are opt-in SHA-checked read-only
   copies. The current-city editor stays intact; Voice candidates are private
   until explicit publication. Launch, backup and rollback commands are in
   [VOICE_RUNBOOK.md](VOICE_RUNBOOK.md); temporary paths in LEGACY_REMOVAL.md.

## Runtime validation

Standalone Docker Compose was run locally with its own Postgres, API, worker,
volume and fake provider. Real HTTP verified readiness, durable 202, textual
result, both outbox stages and retry returning the same reference. No Guide
credentials were supplied to this stack. The container image and wheel built.

Chromium tests exercise a page without Guide CSS/DOM, two widget instances,
permission denial, rerecording, lost 202 retry, retained recording, status and
cleanup. Backend tests use ffmpeg-generated WebM/Ogg/MP4 and WAV. Native Safari,
iOS microphone permissions and Firefox were not exercised in this environment;
verify them before enabling broad visitor traffic.

## Limits before production enablement

Run migrations/backups and the documented archive dry-run on a controlled copy
of real history, compare SHA/counts and resolve missing legacy files. Production
history was not accessed or changed. Live OpenAI credentials/model availability,
provider billing tariffs, sustained load, backup restore and real-browser mobile
recording require operational acceptance. The reservation budget is conservative
internal accounting, not an external provider spending guarantee.

Voice technical settings/run audit is stored in Voice; Guide domain decisions
have atomic before/after entries in Guide. A network failure between services
can require retrying a technical operation; new-run keys are idempotent. Old
trusted sessions/shortcut and Guide voice execution remain intentionally during
transition. No accepted Voice audio is automatically expired.
