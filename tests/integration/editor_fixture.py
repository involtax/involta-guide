"""Seed current pages through a source-backed editorial change, without calling an AI API."""

from uuid import uuid4

from involta_guide.application.services.editorial import EditorialService, save


class FixtureEditor(EditorialService):
    def save(self, location_id, content, expected):
        with self.uow() as work:
            work.repo.lock_locations()
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
                    "ip_hash": "fixture",
                    "device": "fixture",
                    "status": "processed",
                    "moderation": "approved",
                },
            )
            return save(work.repo, location_id, content, expected, review_id=review_id)
