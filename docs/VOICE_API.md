# Voice ↔ Guide API v1

Status: implementation contract, based on `VOICE_SERVICE_SPEC.md@90c396f`.
Guide baseline: `67c77c0`; no changes to city generation or Alex commands.

## Ownership and primary result

Voice has its own database/role, migrations, audio volume, queue and worker.
It must not import Guide runtime code or query Guide tables. Guide supplies a
location/restaurant context with an optimistic concurrency token. Voice cannot publish or moderate Guide.
Alex submissions continue through the existing Guide executor in this release.

The first durable result is the original transcript. A committed
`transcript_ready` outbox event creates one Guide textual review, pending
moderation, before optional analysis. Analysis failure/no_action cannot remove
that review. Original text is immutable; edits have their own revision/history.

## Public gateway (same origin, BASE_PATH configurable)

| Method | Route under `/guide/voice/api/v1` | Contract |
|---|---|---|
| POST | `/identity` | Origin-checked HttpOnly visitor bootstrap; returns server-derived `actor` and admin `csrf_token` |
| POST | `/submissions?location=slug&idempotency_key=UUID&language=ru&timezone=Europe/Moscow` | Binary audio body, `Content-Type: audio/*`; trusted sessions also require `X-CSRF-Token`; 202 after durable audio + queue commit |
| GET/POST | `/debug/{request_id}` | Beta upload diagnostics in existing Guide audit; owner/admin read, Origin-checked browser report write, max 1 MiB |
| GET | `/submissions/mine` | paginated current visitor's history; never all visitors |
| GET | `/submissions/{reference}` | owner-only processing + Guide moderation/publication status |

Guide creates/reads its HttpOnly visitor cookie, validates Origin, rate limits,
resolves the slug, and overwrites all identity/actor/network fields. Browser role
or IP headers have no authority. Browser optional language/timezone are untrusted.
One widget handles both roles. Administrator/Alex cookies select the existing Guide
command executor; browser actor values cannot grant authority. Both roles use the same
upload URL, raw binary transport and widget asset. Multipart remains accepted for open
older clients; the browser never sends multipart through the public proxy.

The widget keeps the blob and idempotency key in IndexedDB, including after reload,
until a valid accepted reference arrives. HTML/non-JSON errors show HTTP status and
the complete response, retaining audio for retry/download. Each attempt has an
`X-Voice-Request-ID`; server receive/response events and the browser's proxy response
are correlated in Guide audit. Credentials/CSRF/cookies and audio are excluded.
Browser diagnostic reports are explicitly untrusted evidence. A failed debug write
cannot invalidate an accepted recording. `VOICE_DEBUG_ENABLED` controls server logging;
beta widget diagnostics remain visible for local failures.

The widget retains the idempotency key and blob when a 202 response is lost.
The same visitor/key/content/context identity returns the original submission;
the same key with another payload returns 409. Equal audio SHA with another key
is accepted as a separate deliberate submission and marked as a repeat signal.

Example accepted response:

```json
{"reference":"550e8400-e29b-41d4-a716-446655440000","state":"accepted","run":1,"transcript":null,"moderation":"pending"}
```

Error envelope: `{"error":{"code":"conflict","message":"…"}}`.
400 invalid data/audio; 401 invalid service credential; 403 origin/authority;
404 missing or non-owned reference (indistinguishable); 409 idempotency/revision;
413 size; 429 intake/queue/API budget; 503 unavailable/disk/storage. No token,
credential, cookie, binary audio or unfiltered source URL in error/log payloads.

## Internal Voice API (`/internal/v1`, bearer service secret)

All routes require `Authorization: Bearer $VOICE_SERVICE_SECRET` over the private
network; public gateway never forwards client Authorization. Visitor identity is
a Guide-computed opaque HMAC, not the browser cookie. Body DTOs/schema version
are defined in `involta_voice.contracts`.

* POST `/submissions`: multipart `audio`, JSON `metadata` (`SubmissionMetadata`).
* GET `/submissions?visitor=…&page=1`: safe owner history.
* GET `/submissions/{reference}?visitor=…`: owner status.
* GET `/admin/submissions`, `/admin/submissions/{reference}`: administrative list/detail.
* GET `/admin/submissions/{reference}/audio`: protected original bytes.
* GET `/admin/submissions/{reference}/runs/{number}`: immutable run snapshot and logs.
* POST `/admin/submissions/{reference}/runs`: deliberate new run; reuse successful transcript, snapshot current settings.
* POST `/admin/submissions/{reference}/runs/{number}/revalidate`: validate saved analysis response; no provider request.
* GET/PUT `/admin/settings`: transcription/analysis models, user analysis prompt and prices.
* GET `/events?after=0&limit=100`: ascending transactional outbox sequence;
  retained immutable events, no GET side effects.
* POST `/events/ack`: acknowledges a committed Guide cursor, never deletes events.

Events have `schema_version=1`, immutable `event_id`, monotonic `sequence`,
`kind` (`transcript_ready`, `analysis_ready`, diagnostic `processing_failed`), submission reference, run number,
metadata snapshot, exact transcript, and optional validated analysis. Analysis
events also contain the transcript so out-of-order delivery can create the base
review. Guide inbox event uniqueness, review/proposal import and cursor update
commit atomically. Ack follows commit. Repeated delivery and ack loss are safe.
Lower run events cannot replace a newer active result or undo decisions.

## Analysis

`AnalysisResult`: `outcome=review|no_action`, reason, `entries=[]`, `suggestions=[]`.
Entries carry exact quote and transcript offsets, restaurant identity/candidates,
optional numeric attributes. Scores never imply an invented rating. Suggestions
are optional candidates (`edit|restaurant`), not privileged commands; explicit
`changes` preserves missing versus null. Unknown restaurants/phone remain pending.
The server checks quote/offset consistency, ID membership, allowed changes,
ranges and non-overlapping attributed fragments. Entire original text survives.

## Editor boundary

Guide actions require its administrator session + CSRF. Every decision supplies
idempotency key, review revision, processing run, proposal revision and expected
current page revision where applicable. 409 means refresh the comparison. Review
text, binding and moderation and proposal decisions have atomic append-only
events with stable actor `guide-editor`, non-token session ID, before/after,
expected/result revisions, run and correlation key. Approval atomically applies the entire corrected review package to the current page; there is no separate publication.

## Recovery and rollout

Worker stages use durable attempts/lease fencing. A completed transcription is
reused. An expired running paid POST becomes `unknown`, not an automatic duplicate
POST. Explicit new processing creates another run. Failed parsing retains raw
responses and supports revalidation. Transcript outbox creation is in the same
transaction as transcription completion; analysis is never a dependency.

The routing flag affects new visitor uploads only. Old jobs and Alex stay with
Guide. Both old and Voice history/audio remain readable with the flag disabled.
Current page state is preserved by the cutover described in CURRENT_STATE.md; obsolete
page snapshots and old generation logs are discarded. Archive
copy is opt-in, repeatable, SHA-checked, and defaults to dry-run. Voice audio has
no automatic retention expiry.

## Frozen schema artifacts and compatibility

Machine-readable schemas: [OpenAPI](voice-openapi.json) and
[DTO JSON Schema](voice-dtos.schema.json). Schema version 1 accepts optional
analysis; the transcript event alone is a complete textual review import.
`processing_failed` is diagnostic and cannot erase an imported transcript.

New-run POST takes an `idempotency_key` UUID plus optional fresh `context`.
Revalidation may recover a saved transcription or analysis; it never calls the
provider. POST `/admin/archive` accepts source UUID, metadata, legacy snapshot
and original audio; SHA must match. It creates a read-only archive, no stages or
outbox. Administrative calls carry a non-secret `X-Guide-Session` UUID for audit.

Alex's GET shortcut now establishes the same administrator cookie as login.
Guide retains the trusted executor and stores `admin_session_id`; existing Alex
sessions remain compatible. Voice receives ordinary visitor submissions only.

The current-city editor is preserved. Review approval applies all remaining
proposals atomically and immediately. `review_revision` is the package revision;
proposal edit/exclusion and newly imported analysis invalidate stale decisions.
`page_revision` and `affected_revisions` fence current and shared-card city data.
No proposals means no empty page version. See VOICE_REVIEW_REPORT.md.

## Local ASR and trusted Guide speech

Speech is local GigaAM / Parakeet through ONNX Runtime; OpenAI receives text for optional analysis only.
POST `/internal/v1/guide-transcriptions` is an authenticated internal multipart
endpoint with `audio` + `SubmissionMetadata` JSON, returning the same status DTO.
Its purpose is forced server-side. Guide/admin tasks use the single Voice worker,
have no analysis stage/outbox event, and never create a second imported review.
They are hidden from visitor/admin review listings; original Guide session roles
and command decisions stay in Guide. Actor `admin` is accepted only on this
internal Guide route; ordinary submission routes still require actor `visitor`.
See ASR_BETA.md for pinned weights, preparation and measured limits.
No proposals means no page mutation. See VOICE_REVIEW_REPORT.md.


## ASR beta contract

New transcription runs schedule a primary `transcription` and independent
`comparison` when `VOICE_ASR_BETA_ENABLED=true`. The primary defaults to GigaAM;
failed primary can use Parakeet. Only one transcript/analysis event chain is
emitted. Comparison errors are private stage diagnostics and never emit Guide
processing_failed events. Admin Guide speech follows the same two-model protocol,
without review import or another command execution. Migration 003 adds comparison
results/annotations; existing transcripts/audio/raw logs remain intact.

Private bearer-authenticated endpoints:

* GET `/admin/submissions/{reference}/asr`: all ASR runs, two candidate states,
  text/error, pinned revision, elapsed seconds, RTF, source engine, audio SHA,
  candidate digest, human evaluation revision and WER/CER (null without reference).
* POST `/admin/submissions/{reference}/asr-runs`:
  `{"idempotency_key":"UUID","force":false}`. Default reuses a matching pair;
  explicit force creates another retained comparison-only run. No active_run,
  original transcript, analysis, moderation or command mutation. Same key with
  different submission/force returns 409. Strict boolean, no unknown fields.
* POST `/admin/submissions/{reference}/asr-runs/{number}/evaluation` requires
  `X-Guide-Session: UUID` and `{idempotency_key, revision, candidates_digest,
  preferred_engine:null|gigaam|parakeet, reference_text:null|string, note:string}`.
  Stale revision/digest or changed idempotent payload returns 409. Preferred model
  must have a successful candidate. Reference ≤30000 chars; note ≤2000. Stores
  actor session, before/after, exact candidate digest/revisions separately from
  review moderation. Does not apply or approve content.

Guide exposes those operations only behind its existing admin cookie + CSRF;
new beta routes do not grant admin privileges. See ASR_BETA.md for inventory,
aggregate reporting, pinned models and the eventual single-model switch.
