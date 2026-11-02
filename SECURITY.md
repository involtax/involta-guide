# Security

Report vulnerabilities privately to the repository owner. Do not include live keys,
private audio, database URLs or Alex links in public issues.

All secrets come from environment/secret manager. Admin passwords use Argon2id;
server sessions store SHA-256 token hashes and support revocation. CSRF and exact
configured Origin checks protect mutating requests. Cookies are HttpOnly, scoped to
/guide, Secure in production, SameSite strict for editors. Existing Alex sessions
remain usable; legacy expiry-only admin cookies are replaced by a fresh login.

Audio is bounded, signature-checked and measured with ffprobe, stored privately under
UUID filenames, accessible only to editors. Visitor/IP limits, pending queue cap,
worker retries and daily API call caps prevent unbounded processing. Raw IP is not
stored. Prompt/web/user content is untrusted; model output cannot authorize roles,
grant itself privileges or publish ordinary review proposals.

Never log query strings (Alex bootstrap), Authorization, environment dumps, upload
bodies or raw provider error bodies. SQLAlchemy hides bind parameters. Production
requires HTTPS and a configured trusted nginx peer; do not trust arbitrary forwarded
headers. Use uvicorn --no-proxy-headers and the app's explicit trusted-proxy list.

Run retention dry-run before execute; exclude pending/active work. Keep a documented
backup retention policy. Production migrations require an explicit task and verified
backup; automatic destructive downgrades are disabled.
