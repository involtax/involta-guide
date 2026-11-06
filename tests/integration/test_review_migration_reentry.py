from alembic import command
from alembic.config import Config
from sqlalchemy import text


def test_review_migration_replaces_orphan_functions_left_by_old_rollback(database):
    settings, engine = database
    config = Config(str(settings.root_dir / "alembic.ini"))
    command.downgrade(config, "hybrid_search")
    with engine.begin() as conn:
        for name in ("apply_review_vote", "invalidate_review_analysis"):
            conn.execute(
                text(
                    f"CREATE FUNCTION {name}() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN RETURN NEW; END $$"
                )
            )
    command.upgrade(config, "head")
    with engine.connect() as conn:
        definition = conn.execute(
            text("SELECT pg_get_functiondef('apply_review_vote()'::regprocedure)")
        ).scalar_one()
        assert "UPDATE restaurants" in definition
        assert conn.execute(text("SELECT to_regclass('review_analyses')")).scalar_one()
        assert conn.execute(text("SELECT count(*) FROM alembic_version")).scalar_one() == 1
