-- Human ASR judgments are deliberately removed; source recordings/results stay.
DROP TABLE asr_evaluation_events;
DROP TABLE asr_evaluations;
UPDATE stages SET state='failed',error='Этот этап отменён: отзывы публикуются из GigaAM без анализа',
 lease_token=NULL,lease_until=NULL,finished_at=now()
 WHERE kind NOT IN ('transcription','comparison') AND state IN ('queued','running');
-- Earlier beta versions could publish the comparison model as a fallback.
-- Historical transcripts stay intact. New events use only proven GigaAM results.
-- Fresh outbox sequences make the existing acknowledged cursor safe to keep.
-- Intake and workers must be stopped for this coordinated migration.
SELECT pg_advisory_xact_lock(47032003);
INSERT INTO outbox(event_id,submission,run,kind,payload)
 SELECT gen_random_uuid(),s.reference,s.active_run,k.kind,
 jsonb_build_object('schema_version',2,'kind',k.kind,'reference',s.reference,'run',s.active_run,
 'metadata',s.metadata,'transcript',CASE WHEN k.kind='gigaam_ready' THEN c.transcript ELSE NULL END,
 'analysis',NULL,'sha256',s.sha256,'mime',s.mime,'duration',s.duration,'created_at',s.created_at,
 'repeat_signals',s.repeat_signals)
 FROM submissions s LEFT JOIN LATERAL (SELECT transcript FROM asr_candidates
   WHERE submission=s.reference AND engine='gigaam' ORDER BY run DESC LIMIT 1) c ON true
 CROSS JOIN (VALUES ('recording_received'),('gigaam_ready')) k(kind)
 WHERE k.kind='recording_received' OR c.transcript IS NOT NULL
 ORDER BY s.created_at,s.reference,k.kind DESC
 ON CONFLICT(submission,run,kind) DO NOTHING;
UPDATE outbox SET payload=payload || jsonb_build_object('event_id',event_id)
 WHERE kind IN ('recording_received','gigaam_ready');
INSERT INTO voice_schema_versions VALUES(6);
