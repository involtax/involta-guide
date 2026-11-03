ALTER TABLE runs ADD COLUMN transcription_engine text;
ALTER TABLE stages DROP CONSTRAINT stages_kind_check;
ALTER TABLE stages ADD CONSTRAINT stages_kind_check CHECK(kind IN ('transcription','analysis','comparison','evaluate_gigaam','evaluate_parakeet'));
CREATE TABLE asr_candidates (
 submission uuid NOT NULL, run integer NOT NULL, engine text NOT NULL CHECK(engine IN ('gigaam','parakeet')),
 revision text NOT NULL, settings jsonb NOT NULL, transcript text NOT NULL,
 elapsed_seconds double precision NOT NULL, duration double precision NOT NULL, details jsonb NOT NULL,
 created_at timestamptz NOT NULL DEFAULT now(), PRIMARY KEY(submission,run,engine),
 FOREIGN KEY(submission,run) REFERENCES runs(submission,number)
);
CREATE TABLE asr_evaluations (
 submission uuid NOT NULL, run integer NOT NULL, revision integer NOT NULL,
 preferred_engine text, reference_text text, note text NOT NULL DEFAULT '',
 PRIMARY KEY(submission,run),FOREIGN KEY(submission,run) REFERENCES runs(submission,number)
);
CREATE TABLE asr_evaluation_events (
 id uuid PRIMARY KEY, submission uuid NOT NULL, run integer NOT NULL, session_id uuid NOT NULL,
 request_hash text NOT NULL,before_data jsonb NOT NULL,after_data jsonb NOT NULL,
 created_at timestamptz NOT NULL DEFAULT now(),FOREIGN KEY(submission,run) REFERENCES runs(submission,number)
);
INSERT INTO voice_schema_versions VALUES(3);
