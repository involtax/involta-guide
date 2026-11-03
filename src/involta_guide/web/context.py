from dataclasses import dataclass

from sqlalchemy import Engine

from involta_guide.application.services.jobs import JobService
from involta_guide.application.services.locations import LocationService, Queries
from involta_guide.config import Settings
from involta_guide.infrastructure.security import Security


@dataclass
class Context:
    settings: Settings
    engine: Engine
    queries: Queries
    locations: LocationService
    jobs: JobService
    security: Security
