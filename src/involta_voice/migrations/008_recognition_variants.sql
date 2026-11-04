-- Preserve every original candidate; four extra variants are queued only on new intake.
ALTER TABLE asr_candidates ADD COLUMN variant text NOT NULL DEFAULT 'onnx'
 CHECK(variant IN ('onnx','sherpa_plain','sherpa_hotwords'));
ALTER TABLE asr_candidates DROP CONSTRAINT asr_candidates_pkey;
ALTER TABLE asr_candidates ADD PRIMARY KEY(submission,run,engine,variant);
ALTER TABLE stages DROP CONSTRAINT stages_kind_check;
ALTER TABLE stages ADD CONSTRAINT stages_kind_check CHECK(kind IN (
 'transcription','analysis','comparison','evaluate_gigaam','evaluate_parakeet',
 'sherpa_gigaam_plain','sherpa_gigaam_hotwords','sherpa_parakeet_plain','sherpa_parakeet_hotwords'
));
INSERT INTO voice_schema_versions VALUES(8);
