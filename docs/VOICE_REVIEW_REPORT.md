# PR8 revision: whole review package

Supersedes the draft/partial-acceptance sections of VOICE_SERVICE_SPEC.md@90c396f.
Review source: docs/pr8-review-fixes/docs/PR8_REVIEW_FIXES.md (2026-11-03).

The application DTO is `application.commands.voice.Decision`. `review_revision`
is the package revision: text, binding, proposal edits/exclusions and arriving
analysis invalidate older screens. `processing_run`, `page_revision`,
`affected_revisions`, session/CSRF and `idempotency_key` fence approval. Existing
readable URLs are retained for the admin agent to reorganize separately.

`approve` validates and applies all remaining proposals in one Guide transaction,
marks the review visible and approved, links actual versions and appends one
before/after decision. An invalid item rolls everything back. Exclusion/edit is
an audited change to the review package, never independent acceptance. Text-only
approval creates no empty page version. Repeated confirmation returns the stored
JSON result. Shared restaurants fence all affected city revisions and preserve
snapshots/links for every affected city. Historical snapshots are not rewritten.

No running code uses candidate-page pointers or separate publication. The old
0006 tables are retained as inert audit data; additive 0007 also merges the
0006_coverage migration from main and introduces consumer diagnostic state.
No applied migrations were edited and no production database was accessed.

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
