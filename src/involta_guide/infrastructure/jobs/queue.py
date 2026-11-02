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
            conn.execute(
                text(
                    "UPDATE jobs SET status=CASE WHEN attempts>=:max THEN 'failed' ELSE 'queued' END,error='Lease expired; previous worker stopped',lease_owner=NULL,lease_until=NULL,available_at=now() WHERE status='running' AND (lease_until<now() OR (lease_until IS NULL AND started_at<now()-interval '20 minutes'))"
                ),
                {"max": self.settings.max_job_attempts},
            )
            row = (
                conn.execute(
                    text(
                        """UPDATE jobs SET status='running',attempts=attempts+1,started_at=now(),lease_owner=:owner,lease_until=now()+make_interval(secs=>:lease) WHERE id=(SELECT id FROM jobs WHERE status='queued' AND available_at<=now() AND attempts<:max ORDER BY created_at,id LIMIT 1 FOR UPDATE SKIP LOCKED) RETURNING *"""
                    ),
                    {
                        "owner": owner,
                        "lease": self.settings.lease_seconds,
                        "max": self.settings.max_job_attempts,
                    },
                )
                .mappings()
                .first()
            )
            return row_dict(row) if row else None

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
            row = (
                conn.execute(
                    text(
                        "UPDATE jobs SET status=CASE WHEN :retry AND attempts<:max THEN 'queued' ELSE 'failed' END,error=:message,finished_at=now(),lease_until=NULL,lease_owner=NULL,available_at=now()+make_interval(secs=>least(60,attempts*5)) WHERE id=:id AND lease_owner=:owner AND status='running' RETURNING review_id,status"
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
            if row and row["review_id"]:
                conn.execute(
                    text("UPDATE reviews SET status=:status,error=:message WHERE id=:id"),
                    {
                        "status": "pending" if row["status"] == "queued" else "failed",
                        "message": message[:2000],
                        "id": row["review_id"],
                    },
                )
                conn.execute(
                    text(
                        "UPDATE alex_changes SET status=:status,detail=:message WHERE review_id=:id AND status<>'applied'"
                    ),
                    {
                        "status": "queued" if row["status"] == "queued" else "failed",
                        "message": message[:2000],
                        "id": row["review_id"],
                    },
                )
