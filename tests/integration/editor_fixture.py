"""Seed current pages through a source-backed editorial change, without calling an AI API."""

from datetime import UTC, datetime
from uuid import uuid4

from involta_guide.application.services.editorial import save


class FixtureEditor:
    def __init__(self, uow):
        self.uow = uow

    def save(self, location_id, content, expected):
        with self.uow() as work:
            location = work.repo.get("locations", location_id, lock=True)
            review_id = str(uuid4())
            work.repo.add(
                "reviews",
                {
                    "id": review_id,
                    "location_id": location_id,
                    "visitor": "fixture-editor",
                    "audio": "fixture.wav",
                    "mime": "audio/wav",
                    "hash": review_id,
                    "ip": "127.0.0.1",
                    "content_epoch": location["content_epoch"],
                    "user_agent": "fixture",
                    "status": "processed",
                    "moderation": "approved",
                },
            )
            result = save(work.repo, location_id, content, expected, review_id=review_id)
            if location["initialized_at"] is None:
                work.repo.change("locations", location_id, {"initialized_at": datetime.now(UTC)})
            for record in work.repo.rows("restaurants", {"location_id": location_id}):
                ranking = next(
                    (r for r in content.get("rankingReport", []) if r["name"] == record["name"]),
                    None,
                )
                if ranking:
                    work.repo.change("restaurants", record["id"], {"ranking": ranking})
            return result


def enqueue_fixture(service, location_id):
    """Seed a specific generation for worker tests; no public/bulk administrative API."""
    with service.uow() as work:
        location = work.repo.get("locations", location_id, lock=True)
        return int(service._queue_generation(work.repo, location) is not None)
