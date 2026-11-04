from sqlalchemy import text
from sqlalchemy.orm import Session

from involta_guide.application.ports.repositories import Record

PUBLIC = """(l.listed OR l.id IN (WITH RECURSIVE useful AS (
 SELECT id,parent_id FROM locations WHERE active=1 AND initialized_at IS NOT NULL
 UNION SELECT p.id,p.parent_id FROM locations p JOIN useful u ON p.id=u.parent_id
 WHERE p.active=1) SELECT id FROM useful))"""


def public_location_catalogue(session: Session) -> list[Record]:
    """A public snapshot; private ranking fields only determine the baseline order."""
    rows = session.execute(
        text(f"""SELECT l.name,l.slug,l.region,l.kind,p.name parent_name
      FROM locations l LEFT JOIN locations p ON p.id=l.parent_id
      WHERE l.active=1 AND {PUBLIC}
      ORDER BY l.initialized_at IS NOT NULL DESC,
      CASE l.priority WHEN 'high' THEN 0 WHEN 'medium' THEN 1 ELSE 2 END,l.name,l.id""")
    ).mappings()
    return [dict(row) for row in rows]
