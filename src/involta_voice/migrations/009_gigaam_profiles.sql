-- Extend the existing result key and stage types; preserve every historical row.
ALTER TABLE asr_candidates DROP CONSTRAINT asr_candidates_variant_check;
ALTER TABLE asr_candidates ADD CONSTRAINT asr_candidates_variant_check CHECK(variant IN (
 'onnx','sherpa_plain','sherpa_hotwords','sherpa_hotwords_weak',
 'sherpa_hotwords_strong','sherpa_hotwords_wide','sherpa_hotwords_city'
));
ALTER TABLE stages DROP CONSTRAINT stages_kind_check;
ALTER TABLE stages ADD CONSTRAINT stages_kind_check CHECK(kind IN (
 'transcription','analysis','comparison','evaluate_gigaam','evaluate_parakeet',
 'sherpa_gigaam_plain','sherpa_gigaam_hotwords','sherpa_parakeet_plain','sherpa_parakeet_hotwords',
 'sherpa_gigaam_hotwords_weak','sherpa_gigaam_hotwords_strong',
 'sherpa_gigaam_hotwords_wide','sherpa_gigaam_hotwords_city'
));
INSERT INTO voice_schema_versions VALUES(9);
