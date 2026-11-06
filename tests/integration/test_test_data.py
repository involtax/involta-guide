import pytest
from sqlalchemy import text

from involta_guide.infrastructure.location_catalog import load_catalog


@pytest.mark.parametrize("iteration", [1, 2])
def test_default_data_is_small_and_mutations_do_not_leak(database, iteration):
    settings, engine = database
    with engine.begin() as connection:
        assert connection.execute(text("SELECT count(*) FROM locations")).scalar_one() < 30
        assert (
            connection.execute(
                text("SELECT intro FROM locations WHERE slug='ivanovo'")
            ).scalar_one()
            == ""
        )
        connection.execute(
            text("UPDATE locations SET intro=:value WHERE slug='ivanovo'"),
            {"value": f"must-not-leak-{iteration}"},
        )


@pytest.mark.full_catalog
def test_full_catalog_marker_keeps_every_real_location(database):
    settings, engine = database
    expected = {row["id"] for row in load_catalog(settings.root_dir)["locations"]}
    with engine.connect() as connection:
        assert set(connection.execute(text("SELECT id FROM locations")).scalars()) == expected
