# PR8 revision: whole review package

Supersedes the draft/partial-acceptance sections of VOICE_SERVICE_SPEC.md@90c396f.
Review source: docs/pr8-review-fixes/docs/PR8_REVIEW_FIXES.md (2026-11-03).

The application DTO is `application.commands.voice.Decision`. `review_revision`
is the package revision: text, binding, proposal edits/exclusions and arriving
analysis invalidate older screens. `processing_run`, `page_revision`,
`affected_revisions`, session/CSRF and `idempotency_key` fence approval. Existing
readable URLs are retained for the admin agent to reorganize separately.

`approve` validates all remaining proposals in one transaction, marks the review
visible and approved, and appends field diffs to `reviews.changes`. The manual
decision records the review/package operation without full page content. An invalid
item rolls everything back. Text-only approval does not touch page state. Repeated
confirmation returns the stored JSON result. Shared restaurants fence all affected
cities and append their diffs to the same source review.

Pages have only current state. The current-state cutover removes the retired
candidate-page tables and all publication snapshots; see CURRENT_STATE.md.

Consumer keeps cursor/inbox/import atomic, ACK follows commit. A bad event blocks
progress and retains its safe identity/type/stack frames. Payloads, exception
messages, cookies, keys and transcript are not logged. Last attempt/success,
heartbeat and error are stored in Guide DB for the admin diagnostic and
`guide voice-consumer-health`; a bounded 3..60s retry resumes after repair.
An outage does not advance the cursor. Existing ack-loss and replay checks remain.

OpenAI uses **OPENAI_API_KEY**; all other Voice settings keep VOICE_ names. The
production overlay forwards the existing variable into Voice, without a second
VOICE_OPENAI_API_KEY copy. Real provider requests remain untested/unused here.

See VOICE_RUNBOOK.md for production preparation and rollback. No merge or
production deployment was performed. Autoapproval remains disabled. Native
Safari/iOS microphone recording, production backup restoration, real model
availability/tariffs and sustained load require separate operational acceptance.

Validation so far: full local regression 243 passed before the extra diagnostic,
key/config and composition cases; targeted package/config/consumer checks passed.
The added whole-package scenarios verify two valid changes together, atomic
rollback for an invalid new restaurant, audited repair/exclusion, stale package
and page conflict, and identical replay without another application/decision.
`proposal_edits` and `excluded_proposals` may also be supplied in the approval
DTO itself; their changes share the same approval transaction and audit.

Additional local checks: 10 focused cases passed, including edits to a shared
restaurant across two cities: both change immediately and the source review records their phone diffs. Production Compose contracts validate
host and bridge modes, immutable application/DB image references, private API,
all seven service definitions, healthchecks and the shared OpenAI variable.

The isolated **production host Compose** smoke passed with generated fake
credentials: Guide and Voice migrations, API upload, Voice worker transcription,
consumer import, admin login and whole-review approval, visible text, then
Voice API shutdown with HTML/history/status still readable. No paid calls.
The reproducible manual harness is tests/deployment/voice_stack_smoke.py; use a
built local VOICE_TEST_IMAGE and a disposable TEST_DATABASE_URL. Host test ports
33080/13100/55439 must be free. Bridge mode has Compose contract validation, not
a full runtime smoke here. Real production deployment was not executed.
