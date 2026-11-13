"""Read-only grouping of fill traces; individual request identities stay unchanged."""

from collections import defaultdict
from datetime import timedelta
from typing import Any


def group_logs(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    by_id = {row["id"]: row for row in rows}
    roots = {row["id"]: row["id"] for row in rows}
    cities: dict[tuple[Any, Any], list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        cities[row["location_id"], row["content_epoch"]].append(row)
        parent = by_id.get(row.get("log_group"))
        if parent and (parent["location_id"], parent["content_epoch"]) == (
            row["location_id"],
            row["content_epoch"],
        ):
            roots[row["id"]] = parent["id"]

    # Older jobs have no explicit group. Only recover the initial sibling pair and
    # research created immediately by a discovery that changed that restaurant.
    # Ambiguous matches stay separate; no historical JSON or identities are rewritten.
    for city in cities.values():
        discoveries = [r for r in city if r["operation"] == "discover" and r["source"] == "server"]
        intros = [r for r in city if r["operation"] == "intro" and r["source"] == "server"]
        for discovery in discoveries:
            if discovery.get("log_group"):
                continue
            matches = [
                r
                for r in intros
                if not r.get("log_group")
                and r["baseline_revision"] == discovery["baseline_revision"]
                and timedelta(0)
                <= discovery["created_at"] - r["created_at"]
                <= timedelta(seconds=1)
            ]
            if len(matches) == 1:
                roots[discovery["id"]] = matches[0]["id"]
        for row in city:
            if (
                row.get("log_group")
                or row["operation"] != "research"
                or row["source"] != "server"
                or row["scope"] != "all"
            ):
                continue
            matches = [
                d
                for d in discoveries
                if d["status"] == "done"
                and d["finished_at"]
                and row["restaurant_id"] in (d["changed_restaurants"] or [])
                and timedelta(0) <= row["created_at"] - d["finished_at"] <= timedelta(seconds=2)
            ]
            if len(matches) == 1:
                roots[row["id"]] = roots[matches[0]["id"]]

    for identity in roots:
        seen = {identity}
        while roots[identity] != roots[roots[identity]]:
            parent = roots[identity]
            if parent in seen:
                roots[identity] = identity
                break
            seen.add(parent)
            roots[identity] = roots[parent]

    groups: dict[str, dict[str, Any]] = {}
    for row in sorted(rows, key=lambda r: (r["created_at"], r["run_number"])):
        identity = roots[row["id"]]
        group = groups.setdefault(
            identity, {**by_id[identity], "members": [], "group_id": identity}
        )
        operation = row["operation"]
        label = {"intro": "Вступление", "discover": "Поиск мест", "research": "Исследование"}.get(
            operation, "Запрос"
        )
        if operation == "research" and row.get("restaurant_name"):
            label += " · " + row["restaurant_name"]
        row["operation_label"] = label
        group["members"].append(row)
    for group in groups.values():
        statuses = {row["status"] for row in group["members"]}
        group["status"] = next(
            (s for s in ("running", "queued", "failed", "cancelled") if s in statuses), "done"
        )
        group["stage_label"] = {
            "running": "В работе",
            "queued": "В очереди",
            "failed": "Есть ошибки",
            "cancelled": "Остановлено",
            "done": "Готово",
        }[group["status"]]
        group["request_count"] = len(group["members"])
        group["signature"] = ",".join(f"{r['id']}:{r['status']}" for r in group["members"])
        group["latest_at"] = max(row["created_at"] for row in group["members"])
    return sorted(groups.values(), key=lambda g: (g["latest_at"], g["id"]), reverse=True)
