# Python migration — audited baseline

## Observed source, not assumed documentation

Baseline: main e96aee9. Runtime was Node 24, public HTML custom HTTP server,
Next 16 administrative UI/API, PostgreSQL 17. Read-only audit of the attached
**development** database found 1,448 locations, 8 restaurants, 2 versions and
0 reviews. No production credentials, schema changes or nginx changes are used.
Legacy migration ledger contains 001–007 with SHA-256 checksums.

| Old implementation | Python destination / parity requirements |
|---|---|
| lib/db.mjs, editorial.mjs | application/editorial service, PostgreSQL UoW; locked location, immutable JSONB versions, explicit publish, restore, review provenance |
| lib/locations.mjs, location-seed.mjs | location service/repository; 1,448 locations, ancestry, hidden Moscow districts, 20 public zones, aliases, relations, shared restaurants |
| lib/ranking.mjs, schema.mjs | pure domain ranking, Pydantic boundary schemas; 8 integer factors, equal weights, half-up arithmetic, unknown score remains unknown |
| lib/public-*.mjs | Jinja public templates; complete HTML, native details, canonical/JSON-LD, sitemap and noindex |
| app/admin, components | HTMX administrative forms/fragments; search, location/content editors, moderation before/after, jobs, prompt/history |
| app/api/reviews | bounded upload, file signature + ffprobe duration, durable private storage, visitor cookie, PostgreSQL queue |
| lib/ai.mjs | HTTPX AI adapter; transcript, web search, structured extraction; fake provider in tests; usage and quota accounting |
| claimJob/processJob | PostgreSQL SKIP LOCKED leases, heartbeats, bounded retry, owner fencing, idempotent application and graceful worker shutdown |
| lib/auth.ts, alex*.mjs | Argon2id, hashed server sessions, CSRF, revocation; Alex human-authorized voice commands + exclusive star + audit |
| proxy.ts | /guide -> 302 /guide/ preserving query; bare-secret Alex bootstrap; no raw query logs |
| public/widget.js, hours.js | retain independent vanilla scripts, same-origin API, actual audio duration validation |
| migrations/001–007.sql | checksum-verified Alembic baseline; never replay already-applied SQL |
| tests/*.mjs | pytest unit/PostgreSQL integration/Playwright E2E; publication, concurrency, queue, AI failure, fraud, geography, security and SEO |
| Dockerfile, CI | Python 3.13 + uv, web/worker containers, existing PostgreSQL, ffprobe, health checks |

## Existing schema and transactional requirements

Preserve names, keys and values for locations, restaurants, versions, reviews,
proposals, jobs, audit, settings, login_attempts, version_reviews,
restaurant_locations, location_relations, location_slug_redirects, alex_sessions,
alex_changes and schema_migrations. All UUID identities and existing JSONB
snapshots remain readable. Existing integer booleans remain integer columns.
Versions have unique (location_id,number) and (location_id,id); locations point
to their own versions through composite FKs. Reviews/proposals/jobs retain FKs.
Active generation/review jobs have partial unique indexes. All relation tables
have composite primary keys. Existing tree-cycle and slug/alias triggers remain.

Lock the location row for version numbering and current-page updates. Every edit
carries the expected current revision. Saving updates the public page atomically.
AI captures this revision before network work and checks it afterwards. Historical
snapshots remain immutable and are restored by creating a new current snapshot.

## Safe transition to Alembic

1. Stop old workers and writes during cutover. Take pg_dump custom-format backup
   and archive private audio. Record row counts and schema-only dump privately.
2. Restore into an isolated PostgreSQL database; set DATABASE_URL to that copy.
3. Run `uv run guide schema-audit` and `uv run alembic upgrade head` on the copy.
   Baseline checks every legacy checksum and only executes missing migrations;
   a database with untracked existing tables is rejected. Alembic adds its own
   version ledger and additive session/lease/usage columns/tables only.
4. Run counts, FK checks, integration and E2E tests on the copy. Import is explicit,
   idempotent and never replaces existing location fields or identity.
5. Deploy the Python image only after successful validation and an explicit
   production migration task. This implementation does not change production.
6. Run migrations once as a separate release command, then web and workers.
7. Verify /guide -> 302 /guide/, login/CSRF, published versions, a test upload,
   moderation, queue/worker, sitemap and absence of frontend build scripts.
8. Roll back application image to the old release if necessary; stop Python
   workers first. Additive columns can remain. Do not downgrade/drop live tables.
   Restore backup only if data restoration is explicitly authorized, accounting
   for writes since cutover. No destructive automatic downgrade is provided.

## Deliberate hardening versus old behavior

Server-side revocable admin sessions replace signed expiry-only cookies. Audio
length is measured by ffprobe rather than trusting browser duration. Lease owner
fencing prevents stale worker results. Shared restaurant cards use published
snapshots, not unpublished mutable descriptions. Version reproduction includes
historical stars. New uploads and AI calls have quotas. HTML caching is deliberately disabled for this first migration: each request reads
the current published snapshot, avoiding stale hierarchy and personalized Alex markup.
A later cache must include published revision and location metadata.

## Verification

Implementation and command results are recorded here after running Python checks.
Old Node implementation is retained until Python parity tests pass, then removed
from the runtime/repository; Git history remains the rollback reference.

### Executed verification (development only)

- Python 3.13.16, uv.lock resolved/frozen; Ruff check + format and mypy pass.
- 28 pytest cases pass, including real PostgreSQL and Chromium/MediaRecorder E2E.
- Legacy-ledger adoption test preserves counts, UUIDs, version JSON and review FKs.
- Concurrent edits, SKIP LOCKED claims, stale leases, bounded retry, fake provider
  usage/budget, manual moderation, Alex session revocation and star permissions pass.
- Docker image builds successfully with ffprobe; no Node binary in production image.
- Non-root container readiness (200) and schema audit pass against the development copy.
  File permissions are normalized in the image; source environment permissions are not assumed.
- Protected `var/backups/development-before-python.dump` created locally. A separate
  `guide_python_preview` database was restored and upgraded; original `guide` was
  left unchanged. Preview retains 1,448 locations, 8 restaurants, 2 versions,
  0 reviews and all seven legacy migration records.
- Old Next/React/Node source and package manifests removed only after Python tests
  and image build passed. Git history e96aee9 remains the rollback reference.
- GitHub Actions is configured to run the same checks and Docker build; its remote
  run is not asserted here before the workflow executes on GitHub.
- No production nginx/database changes and no paid OpenAI calls were performed.
