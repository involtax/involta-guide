-- Publish existing terminal primary failures through the same durable delivery path.
-- Source recordings, provider responses and the acknowledged cursor are retained.
SELECT pg_advisory_xact_lock(47032003);
INSERT INTO outbox(event_id,submission,run,kind,payload)
 SELECT gen_random_uuid(),s.reference,s.active_run,'gigaam_failed',
 jsonb_build_object('schema_version',2,'kind','gigaam_failed','reference',s.reference,
 'run',s.active_run,'metadata',s.metadata,'transcript',NULL,'analysis',NULL,
 'error',p.error,'sha256',s.sha256,'mime',s.mime,'duration',s.duration,
 'created_at',s.created_at,'repeat_signals',s.repeat_signals)
 FROM submissions s JOIN stages p ON p.submission=s.reference AND p.run=s.active_run
 WHERE p.kind='transcription' AND p.state IN ('failed','unknown')
 AND NOT EXISTS(SELECT 1 FROM asr_candidates c WHERE c.submission=s.reference AND c.engine='gigaam' AND length(trim(c.transcript))>0)
 ON CONFLICT(submission,run,kind) DO NOTHING;
UPDATE outbox SET payload=payload || jsonb_build_object('event_id',event_id)
 WHERE kind='gigaam_failed' AND NOT (payload ? 'event_id');
INSERT INTO voice_schema_versions VALUES(7);
