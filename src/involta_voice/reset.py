"""Owner-local epoch fencing and restartable file deletion; no archive or tombstones."""

from typing import Any

from sqlalchemy import Connection

from .db import execute, row
from .errors import VoiceError


def lock_epoch(conn: Connection, location_id: str, epoch: int) -> None:
    execute(
        conn,
        "INSERT INTO location_epochs(location_id,content_epoch) VALUES(:id,1) ON CONFLICT DO NOTHING",
        id=location_id,
    )
    state = row(
        conn, "SELECT * FROM location_epochs WHERE location_id=:id FOR UPDATE", id=location_id
    )
    if not state or state["reset_pending"] or state["content_epoch"] != epoch:
        raise VoiceError("Локация сброшена. Запишите новый отзыв.", "stale_epoch", 409)


def purge(service: Any, location_id: str, epoch: int) -> dict[str, Any]:
    with service.engine.begin() as conn:
        execute(
            conn,
            "INSERT INTO location_epochs(location_id,content_epoch) VALUES(:id,:epoch) ON CONFLICT DO NOTHING",
            id=location_id,
            epoch=epoch,
        )
        state = row(
            conn, "SELECT * FROM location_epochs WHERE location_id=:id FOR UPDATE", id=location_id
        )
        assert state
        if epoch < state["content_epoch"]:
            raise VoiceError("Устаревший сброс", "stale_epoch", 409)
        if state["purged_epoch"] >= epoch:
            return {"purged": True, "content_epoch": epoch}
        execute(
            conn,
            "UPDATE location_epochs SET content_epoch=:epoch,reset_pending=true WHERE location_id=:id",
            epoch=epoch,
            id=location_id,
        )
        # Include orphan/temporary files after ambiguous intake commits. New filenames carry
        # owner+epoch, and intake holds this same row lock through file creation/commit.
        paths = {
            r[0]
            for r in conn.exec_driver_sql(
                "SELECT audio FROM submissions WHERE location_id=%s AND content_epoch<%s",
                (location_id, epoch),
            )
        }
        for path in service.storage.root.glob(location_id + ".*"):
            parts = path.name.split(".")
            if len(parts) >= 4 and parts[1].isdigit() and int(parts[1]) < epoch:
                paths.add(path.name)
        for path in sorted(paths):
            execute(
                conn,
                "INSERT INTO purge_files(path,location_id,content_epoch) VALUES(:path,:id,:epoch) ON CONFLICT DO NOTHING",
                path=path,
                id=location_id,
                epoch=epoch,
            )
        execute(
            conn,
            "DELETE FROM intake_windows WHERE location_id=:id AND content_epoch<:epoch",
            id=location_id,
            epoch=epoch,
        )
        condition = "submission IN (SELECT reference FROM submissions WHERE location_id=:id AND content_epoch<:epoch)"
        for table in [
            "asr_run_requests",
            "asr_candidates",
            "outbox",
            "provider_calls",
            "stages",
            "runs",
        ]:
            execute(conn, f"DELETE FROM {table} WHERE {condition}", id=location_id, epoch=epoch)
        execute(
            conn,
            "DELETE FROM admin_events WHERE location_id=:id AND content_epoch<:epoch",
            id=location_id,
            epoch=epoch,
        )
        execute(
            conn,
            "DELETE FROM submissions WHERE location_id=:id AND content_epoch<:epoch",
            id=location_id,
            epoch=epoch,
        )
    # The durable manifest remains if any unlink fails; retries never lose the file names.
    cleanup_files(service, location_id, epoch)
    with service.engine.begin() as conn:
        state = row(
            conn, "SELECT * FROM location_epochs WHERE location_id=:id FOR UPDATE", id=location_id
        )
        if not state or state["content_epoch"] != epoch:
            raise VoiceError("Цикл изменён", "stale_epoch", 409)
        execute(
            conn,
            "UPDATE location_epochs SET reset_pending=false,purged_epoch=:epoch WHERE location_id=:id",
            id=location_id,
            epoch=epoch,
        )
    return {"purged": True, "content_epoch": epoch}


def cleanup_files(service: Any, location_id: str | None = None, epoch: int | None = None) -> None:
    from sqlalchemy import text

    with service.engine.connect() as conn:
        paths = (
            conn.execute(
                text(
                    "SELECT path FROM purge_files WHERE (CAST(:id AS uuid) IS NULL OR location_id=CAST(:id AS uuid)) AND (CAST(:epoch AS bigint) IS NULL OR content_epoch<=CAST(:epoch AS bigint))"
                ),
                {"id": location_id, "epoch": epoch},
            )
            .scalars()
            .all()
        )
    for path in paths:
        service.storage.path(path).unlink(missing_ok=True)
        # The storage's interrupted atomic-write name is also owner data.
        if not path.endswith(".upload"):
            service.storage.path(path + ".upload").unlink(missing_ok=True)
        with service.engine.begin() as conn:
            execute(conn, "DELETE FROM purge_files WHERE path=:path", path=path)


def lock_submission(conn: Connection, reference: str) -> dict[str, Any]:
    submission = row(conn, "SELECT * FROM submissions WHERE reference=:id", id=reference)
    if not submission:
        raise VoiceError("Запись не найдена", "not_found", 404)
    lock_epoch(conn, str(submission["location_id"]), submission["content_epoch"])
    submission = row(conn, "SELECT * FROM submissions WHERE reference=:id FOR UPDATE", id=reference)
    if not submission:
        raise VoiceError("Запись не найдена", "not_found", 404)
    return submission
