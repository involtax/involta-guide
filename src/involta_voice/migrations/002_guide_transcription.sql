ALTER TABLE submissions ADD COLUMN purpose text NOT NULL DEFAULT 'review' CHECK(purpose IN ('review','guide_transcription'));
INSERT INTO voice_schema_versions VALUES(2);
