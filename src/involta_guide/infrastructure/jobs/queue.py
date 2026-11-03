from uuid import uuid4

from sqlalchemy import Engine, text

from involta_guide.application.ports.repositories import Record
from involta_guide.config import Settings
from involta_guide.infrastructure.persistence.repositories.postgres import row_dict


class JobQueue:
    def __init__(self, engine: Engine, settings: Settings) -> None:
        self.engine = engine
        self.settings = settings

    def claim(self) -> Record | None:
        owner = str(uuid4())
        with self.engine.begin() as conn:
            # Serialize the capacity decision across workers; no lock spans an API request.
            conn.execute(text("SELECT pg_advisory_xact_lock(47031010)"))
            conn.execute(
                text(
                    "UPDATE jobs j SET status=CASE WHEN attempts>=:max AND NOT EXISTS(SELECT 1 FROM generation_requests g WHERE g.job_id=j.id) THEN 'failed' ELSE 'queued' END,error='Lease expired; previous worker stopped',lease_owner=NULL,lease_until=NULL,available_at=now() WHERE status='running' AND (lease_until<now() OR (lease_until IS NULL AND started_at<now()-interval '20 minutes'))"
                ),
                {"max": self.settings.max_job_attempts},
            )
            row = (
                conn.execute(
                    text(
                        """UPDATE jobs j SET status='running',
                        attempts=attempts+CASE WHEN EXISTS(SELECT 1 FROM generation_requests g WHERE g.job_id=j.id) THEN 0 ELSE 1 END,
                        started_at=coalesce(started_at,now()),lease_owner=:owner,
                        lease_until=now()+make_interval(secs=>:lease)
                        WHERE id=(SELECT q.id FROM jobs q WHERE status='queued' AND available_at<=now()
                          AND (attempts<:max OR EXISTS(SELECT 1 FROM generation_requests g WHERE g.job_id=q.id))
                          AND (kind<>'generate' OR EXISTS(SELECT 1 FROM generation_requests g WHERE g.job_id=q.id)
                            OR (SELECT count(*) FROM jobs active WHERE active.kind='generate'
                                AND (active.status='running' OR (active.status='queued' AND EXISTS(
                                  SELECT 1 FROM generation_requests g WHERE g.job_id=active.id))))<:parallel)
                        ORDER BY available_at,created_at,id LIMIT 1 FOR UPDATE SKIP LOCKED)
                        RETURNING j.*,(EXISTS(SELECT 1 FROM generation_requests g WHERE g.job_id=j.id)) AS background_resume"""
                    ),
                    {
                        "owner": owner,
                        "lease": self.settings.lease_seconds,
                        "max": self.settings.max_job_attempts,
                        "parallel": self.settings.generation_parallel_limit,
                    },
                )
                .mappings()
                .first()
            )
            return row_dict(row) if row else None

    def defer(self, job: Record, message: str = "") -> None:
        with self.engine.begin() as conn:
            conn.execute(
                text("""UPDATE jobs SET status='queued',error=:error,finished_at=NULL,
                available_at=now()+make_interval(secs=>:delay),lease_until=NULL,lease_owner=NULL
                WHERE id=:id AND lease_owner=:owner AND status='running'"""),
                {
                    "id": job["id"],
                    "owner": job["lease_owner"],
                    "error": message or None,
                    "delay": self.settings.generation_poll_seconds,
                },
            )

    def heartbeat(self, job: Record) -> bool:
        with self.engine.begin() as conn:
            result = conn.execute(
                text(
                    "UPDATE jobs SET lease_until=now()+make_interval(secs=>:lease) WHERE id=:id AND lease_owner=:owner AND status='running' AND lease_until>now()"
                ),
                {
                    "lease": self.settings.lease_seconds,
                    "id": job["id"],
                    "owner": job["lease_owner"],
                },
            )
            return result.rowcount == 1

    def fail(self, job: Record, message: str, *, retry: bool = True) -> None:
        with self.engine.begin() as conn:
            (
                conn.execute(
                    text(
                        "UPDATE jobs SET status=CASE WHEN :retry AND attempts<:max THEN 'queued' ELSE 'failed' END,error=:message,finished_at=now(),lease_until=NULL,lease_owner=NULL,available_at=now()+make_interval(secs=>least(60,attempts*5)) WHERE id=:id AND lease_owner=:owner AND status='running' RETURNING status"
                    ),
                    {
                        "retry": retry,
                        "max": self.settings.max_job_attempts,
                        "message": message[:2000],
                        "id": job["id"],
                        "owner": job["lease_owner"],
                    },
                )
                .mappings()
                .first()
            )
            if job["kind"] == "reset":
                conn.execute(
                    text(
                        "UPDATE locations SET reset_error=:error WHERE id=:id AND content_epoch=:epoch AND reset_pending"
                    ),
                    {
                        "error": message[:2000],
                        "id": job["location_id"],
                        "epoch": job["content_epoch"],
                    },
                )
