"""Read-only ownership audit for a prospective universal source removal."""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from dashboard.event_store import EventStore
from dashboard.external_source_config import (
    _locked_configuration_write,
    load_external_sources_configuration,
)
from dashboard.single_source_import import source_import_lock
from dashboard.built_in_signal_registry import BUILT_IN_SIGNAL_TYPE_REGISTRY


@dataclass(frozen=True, slots=True)
class RemovalPreflight:
    source_id: str
    display_name: str
    owned_events: int
    exclusive_types: tuple[str, ...]
    retained_types: tuple[str, ...]
    fingerprint: str


def preflight_source_removal(
    configuration_path: Path,
    source_id: str,
    store: EventStore,
    *,
    connection: sqlite3.Connection | None = None,
) -> RemovalPreflight:
    """Audit ownership without changing the configuration or EventStore."""
    config = load_external_sources_configuration(configuration_path)
    source = next(
        (item for item in config.sources if item.source_id == source_id), None
    )
    if source is None or source.source_origin != "universal_wizard_v1":
        raise ValueError("Only universal wizard sources are eligible")
    if any(item.source_id == source_id for item in config.custom_signals):
        raise ValueError("Source is referenced by a custom signal")
    prefix = f"external:{source_id}:"
    rows: list[tuple[int, str, str, str | None]] = []
    retained_event_types: set[str] = set()
    if connection is not None:
        rows, retained_event_types = _audit(connection, source_id, prefix)
    elif store.database_path.exists():
        uri = store.database_path.resolve().as_uri() + "?mode=ro"
        with sqlite3.connect(uri, uri=True) as connection:
            if _marker(connection, source_id) is not None:
                raise ValueError("Removal already committed; inspect status and resume")
            rows, retained_event_types = _audit(connection, source_id, prefix)
    mapped = set(source.type_mapping.values())
    custom = set(config.signal_types.by_event_type) - set(
        BUILT_IN_SIGNAL_TYPE_REGISTRY.by_event_type
    )
    remaining_mapped = {
        value
        for other in config.sources
        if other.source_id != source_id
        for value in other.type_mapping.values()
    }
    exclusive = mapped & custom - remaining_mapped - retained_event_types
    retained = mapped - exclusive
    raw = json.loads(configuration_path.read_text(encoding="utf-8"))
    for item in raw["sources"]:
        if item.get("id") == source_id:
            item.pop("pending_removal", None)
            item.pop("removal_fingerprint", None)
    snapshot = {
        "configuration": raw,
        "rows": rows,
        "retained_event_types": sorted(retained_event_types),
    }
    fingerprint = hashlib.sha256(
        json.dumps(snapshot, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    return RemovalPreflight(
        source_id,
        source.display_name,
        len(rows),
        tuple(sorted(exclusive)),
        tuple(sorted(retained)),
        fingerprint,
    )


def _audit(
    connection: sqlite3.Connection, source_id: str, prefix: str
) -> tuple[list[tuple[int, str, str, str | None]], set[str]]:
    columns = {row[1] for row in connection.execute("PRAGMA table_info(signal_events)")}
    if "owner_source_id" not in columns:
        raise ValueError("Event ownership is unavailable")
    rows = connection.execute(
        "SELECT event_id, delivery_identity, event_type, owner_source_id "
        "FROM signal_events WHERE owner_source_id = ? OR "
        "substr(delivery_identity, 1, ?) = ? ORDER BY event_id",
        (source_id, len(prefix), prefix),
    ).fetchall()
    if any(
        owner != source_id or not identity.startswith(prefix)
        for _, identity, _, owner in rows
    ):
        raise ValueError("Source event ownership is missing or contradictory")
    retained = {
        row[0]
        for row in connection.execute(
            "SELECT DISTINCT event_type FROM signal_events "
            "WHERE owner_source_id IS NULL OR owner_source_id != ?",
            (source_id,),
        )
    }
    return rows, retained


def _write_configuration(path: Path, raw: dict[str, Any]) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    try:
        with temporary.open("w", encoding="utf-8") as output:
            json.dump(raw, output, indent=2)
            output.write("\n")
            output.flush()
            os.fsync(output.fileno())
        load_external_sources_configuration(temporary)
        os.replace(temporary, path)
        fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    finally:
        temporary.unlink(missing_ok=True)


def _marker(connection: sqlite3.Connection, source_id: str) -> tuple[str, str] | None:
    exists = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='source_removals'"
    ).fetchone()
    if not exists:
        return None
    result = connection.execute(
        "SELECT fingerprint, candidates FROM source_removals WHERE source_id = ?",
        (source_id,),
    ).fetchone()
    return (result[0], result[1]) if result else None


def removal_status(path: Path, source_id: str, store: EventStore) -> dict[str, object]:
    """Expose the original confirmation token for an interrupted operation."""
    raw = json.loads(path.read_text(encoding="utf-8"))
    matches = [item for item in raw["sources"] if item.get("id") == source_id]
    if len(matches) > 1:
        raise ValueError("Source identity is ambiguous")
    marker = None
    if store.database_path.exists():
        uri = store.database_path.resolve().as_uri() + "?mode=ro"
        with sqlite3.connect(uri, uri=True) as connection:
            marker = _marker(connection, source_id)
    if marker:
        return {
            "status": "committed" if matches else "completed",
            "fingerprint": marker[0],
            "plan": json.loads(marker[1]),
        }
    if matches and matches[0].get("pending_removal"):
        return {
            "status": "pending",
            "fingerprint": matches[0].get("removal_fingerprint"),
        }
    if not matches:
        raise ValueError("Source was not found")
    return {"status": "ready"}


def execute_source_removal(
    configuration_path: Path,
    source_id: str,
    store: EventStore,
    lock_directory: Path,
    *,
    confirmed: bool,
    fingerprint: str,
) -> str:
    """Confirm or resume removal. Lock order is source, JSON, then SQLite."""
    if confirmed is not True or not isinstance(fingerprint, str) or not fingerprint:
        raise ValueError("Explicit confirmation and fingerprint are required")
    if not isinstance(source_id, str) or not source_id:
        raise ValueError("source_id is required")
    with source_import_lock(lock_directory, source_id, wait=True):
        return _execute_locked(configuration_path, source_id, store, fingerprint)


@_locked_configuration_write
def _execute_locked(
    path: Path, source_id: str, store: EventStore, fingerprint: str
) -> str:
    raw = json.loads(path.read_text(encoding="utf-8"))
    matches = [s for s in raw["sources"] if s.get("id") == source_id]
    if len(matches) > 1:
        raise ValueError("Source identity is ambiguous")
    source = matches[0] if matches else None
    if source is not None and source.get("source_origin") != "universal_wizard_v1":
        raise ValueError("Only universal wizard sources are eligible")
    if any(
        isinstance(item, dict) and item.get("source_id") == source_id
        for item in raw.get("custom_signals", [])
    ):
        raise ValueError("Source is referenced by a custom signal")

    # Initialize only as part of confirmed execution, never during preflight.
    if not store.database_path.exists():
        if source is None:
            raise ValueError("Source was not found")
        if preflight_source_removal(path, source_id, store).fingerprint != fingerprint:
            raise ValueError("Removal plan changed; run preflight again")
        store.initialize()
    with sqlite3.connect(store.database_path) as connection:
        marker = _marker(connection, source_id)
        if marker is not None:
            if marker[0] != fingerprint:
                raise ValueError(
                    "Removal fingerprint does not match committed operation"
                )
            if source is None:
                return "completed"
            if (
                not source.get("pending_removal")
                or source.get("removal_fingerprint") != fingerprint
            ):
                raise ValueError(
                    "Committed removal conflicts with source configuration"
                )
            saved_plan = json.loads(marker[1])
            _finalize(path, raw, source_id, saved_plan["exclusive_types"], connection)
            return "completed"
        if source is None:
            raise ValueError("Source was not found")
        existing_fingerprint = source.get("removal_fingerprint")
        if existing_fingerprint and existing_fingerprint != fingerprint:
            raise ValueError("A different removal is pending")
        if preflight_source_removal(path, source_id, store).fingerprint != fingerprint:
            raise ValueError("Removal plan changed; run preflight again")
        if not source.get("pending_removal") or not existing_fingerprint:
            source["pending_removal"] = True
            source["removal_fingerprint"] = fingerprint
            _write_configuration(path, raw)
        connection.execute("BEGIN IMMEDIATE")
        try:
            plan = preflight_source_removal(
                path, source_id, store, connection=connection
            )
            if plan.fingerprint != fingerprint:
                raise ValueError("Removal plan changed; cancel or run preflight again")
            connection.execute(
                "CREATE TABLE IF NOT EXISTS source_removals "
                "(source_id TEXT PRIMARY KEY, fingerprint TEXT NOT NULL, "
                "candidates TEXT NOT NULL)"
            )
            connection.execute(
                "DELETE FROM signal_events WHERE owner_source_id = ?", (source_id,)
            )
            connection.execute(
                "INSERT INTO source_removals VALUES (?, ?, ?)",
                (source_id, fingerprint, json.dumps(asdict(plan))),
            )
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        _finalize(path, raw, source_id, list(plan.exclusive_types), connection)
        return "completed"


def _finalize(
    path: Path,
    raw: dict[str, Any],
    source_id: str,
    candidates: list[str],
    connection: sqlite3.Connection,
) -> None:
    # Keep a write transaction until JSON is durable so retained events cannot
    # change while the final type cleanup is decided.
    connection.execute("BEGIN IMMEDIATE")
    try:
        sources = [s for s in raw["sources"] if s.get("id") != source_id]
        used = {
            value
            for source in sources
            for value in source.get("type_mapping", {}).values()
        }
        used.update(
            row[0]
            for row in connection.execute(
                "SELECT DISTINCT event_type FROM signal_events"
            )
        )
        builtins = set(THIRD_SIGNAL_TEST_REGISTRY.by_event_type)
        remove = set(candidates) - used - builtins
        raw["sources"] = sources
        raw["signal_types"] = [
            item for item in raw["signal_types"] if item.get("id") not in remove
        ]
        overrides = raw.get("signal_type_display_overrides", {})
        for type_id in remove:
            overrides.pop(type_id, None)
        _write_configuration(path, raw)
        connection.commit()
    except BaseException:
        connection.rollback()
        raise


def cancel_source_removal(
    configuration_path: Path,
    source_id: str,
    store: EventStore,
    lock_directory: Path,
    *,
    confirmed: bool,
) -> None:
    if confirmed is not True:
        raise ValueError("Explicit confirmation is required")
    with source_import_lock(lock_directory, source_id, wait=True):
        _cancel_locked(configuration_path, source_id, store)


@_locked_configuration_write
def _cancel_locked(path: Path, source_id: str, store: EventStore) -> None:
    if store.database_path.exists():
        uri = store.database_path.resolve().as_uri() + "?mode=ro"
        with sqlite3.connect(uri, uri=True) as connection:
            if _marker(connection, source_id) is not None:
                raise ValueError(
                    "Removal already committed; cancellation is unavailable"
                )
    raw = json.loads(path.read_text(encoding="utf-8"))
    matches = [s for s in raw["sources"] if s.get("id") == source_id]
    if len(matches) != 1 or matches[0].get("source_origin") != "universal_wizard_v1":
        raise ValueError("Only universal wizard sources are eligible")
    if not matches[0].get("pending_removal"):
        raise ValueError("Removal is not pending")
    matches[0].pop("pending_removal", None)
    matches[0].pop("removal_fingerprint", None)
    _write_configuration(path, raw)
