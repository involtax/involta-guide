import json
from copy import deepcopy
from datetime import datetime, timedelta
from pathlib import Path

from involta_guide.infrastructure.ai.log_groups import group_logs


def chelny():
    rows = json.loads((Path(__file__).parents[1] / "fixtures/chelny-log-metadata.json").read_text())
    for row in rows:
        for field in ("created_at", "finished_at"):
            row[field] = datetime.fromisoformat(row[field]) if row[field] else None
    return rows


def test_existing_chelny_twenty_requests_form_one_group_without_rewriting_rows():
    rows = chelny()
    groups = group_logs(deepcopy(rows))
    assert len(groups) == 1
    assert groups[0]["id"] == rows[0]["id"]
    assert groups[0]["request_count"] == 20
    assert len({r["restaurant_id"] for r in groups[0]["members"][2:]}) == 18
    assert groups[0]["members"][3]["operation_label"] == "Исследование · Ике"
    assert (
        "log_group" not in groups[0]["members"][0] or groups[0]["members"][0]["log_group"] is None
    )


def test_later_manual_full_research_is_not_folded_into_old_fill():
    rows = chelny()
    manual = {
        **rows[2],
        "id": "manual",
        "run_number": 21,
        "created_at": rows[-1]["finished_at"] + timedelta(minutes=1),
    }
    rows.append(manual)
    groups = group_logs(rows)
    assert len(groups) == 2
    assert groups[0]["id"] == "manual" and groups[0]["request_count"] == 1


def test_ambiguous_old_parent_is_left_separate_and_epochs_do_not_mix():
    rows = chelny()
    duplicate = {**rows[1], "id": "duplicate", "run_number": 21}
    groups = group_logs([*rows, duplicate])
    assert len(groups) == 19  # Ambiguous discovery parent: do not guess for its 18 children.
    rows = chelny()
    rows[1]["content_epoch"] = "another-epoch"
    assert len(group_logs(rows)) == 20


def test_new_children_of_an_old_queued_discovery_keep_the_same_recovered_group():
    rows = chelny()
    for child in rows[2:]:
        child["log_group"] = rows[1]["id"]
    groups = group_logs(rows)
    assert len(groups) == 1 and groups[0]["request_count"] == 20
