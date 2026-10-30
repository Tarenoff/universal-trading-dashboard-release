"""Explicit, isolated preflight and one-time import for a selected source."""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from dashboard.event_store import EventStore
from dashboard.external_source_config import (
    load_external_sources_configuration,
    set_preparatory_removal,
)
from dashboard.external_sqlite_import import (
    ExternalSqliteSource,
    import_external_sqlite_events,
    inspect_external_sqlite_columns,
    inspect_external_sqlite_tables,
    preview_external_sqlite_rows,
)
from dashboard.source_metrics import record_successful_source_import


@dataclass(frozen=True, slots=True)
class SourceImportPreflight:
    source_id: str
    display_name: str
    database_path: Path
    table: str
    type_mapping: dict[str, str]
    exchange: str
    metadata_columns: dict[str, str]
    fingerprint: str


@dataclass(frozen=True, slots=True)
class SingleSourceImportResult:
    scanned: int | None = None
    inserted: int | None = None
    rejected: int | None = None
    error: str | None = None


@contextmanager
def source_import_lock(
    lock_directory: Path, source_id: str, *, wait: bool
) -> Iterator[None]:
    """Acquire source lock before the JSON writer lock; never reverse this order."""
    lock_directory.mkdir(parents=True, exist_ok=True)
    name = hashlib.sha256(source_id.encode()).hexdigest() + ".lock"
    fd = os.open(lock_directory / name, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "r+") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX | (0 if wait else fcntl.LOCK_NB))
        yield


def prepare_source_removal(
    configuration_path: Path,
    source_id: str,
    lock_directory: Path,
    *,
    confirmed: bool,
    pending: bool = True,
) -> None:
    """Wait for import, then persist or cancel this reversible preparation only."""
    if confirmed is not True:
        raise ValueError("Explicit confirmation is required")
    if not isinstance(source_id, str) or not source_id:
        raise ValueError("source_id is required")
    if not pending:
        raise ValueError("Cancellation requires the EventStore completion check")
    with source_import_lock(lock_directory, source_id, wait=True):
        set_preparatory_removal(configuration_path, source_id, pending)


def _resolve_and_validate(
    configuration_path: Path,
    source_id: str,
    store: EventStore,
    allowed_roots: Sequence[Path | str] | None,
) -> tuple[ExternalSqliteSource, SourceImportPreflight]:
    configuration = load_external_sources_configuration(configuration_path)
    source = next(
        (item for item in configuration.sources if item.source_id == source_id), None
    )
    if source is None:
        raise ValueError("Source ID was not found")
    if source.pending_removal:
        raise ValueError("Source has pending removal")
    source_path = source.database_path.resolve()
    destination_path = store.database_path.resolve()
    if source_path == destination_path or (
        source_path.exists()
        and destination_path.exists()
        and os.path.samefile(source_path, destination_path)
    ):
        raise ValueError("Source and destination databases must be separate")
    if source.exchange not in {"", "BYBIT", "BINANCE_FUTURES"}:
        raise ValueError("Unsupported source exchange")
    if set(source.type_mapping.values()) - set(store.allowed_event_types):
        raise ValueError("Source maps to an unregistered destination type")
    if (
        source.fixed_signal_type is not None
        and source.fixed_signal_type not in source.type_mapping
    ):
        raise ValueError("Fixed signal type is missing from the mapping")
    if any(not value.strip() for value in source.type_mapping):
        raise ValueError("Source type values must not be empty")

    tables = inspect_external_sqlite_tables(
        source.database_path, allowed_roots=allowed_roots
    )
    if source.table_name not in tables:
        raise ValueError("Source table was not found")
    columns = {
        item["name"]
        for item in inspect_external_sqlite_columns(
            source.database_path, source.table_name, allowed_roots=allowed_roots
        )
    }
    required = {
        source.primary_key_column,
        source.symbol_column,
        source.timestamp_column,
    }
    required.update(source.metadata_columns.values())
    if source.signal_type_column is not None:
        required.add(source.signal_type_column)
    if source.price_column is not None:
        required.add(source.price_column)
    if source.exchange_column is not None:
        required.add(source.exchange_column)
    if missing := sorted(required - columns):
        raise ValueError(f"Source columns not found: {', '.join(missing)}")
    preview_external_sqlite_rows(
        source.database_path,
        source.table_name,
        sorted(required),
        allowed_roots=allowed_roots,
        limit=1,
    )
    snapshot = {
        "id": source.source_id,
        "source_origin": source.source_origin,
        "name": source.display_name,
        "database_path": str(source.database_path.resolve()),
        "table": source.table_name,
        "id_column": source.primary_key_column,
        "symbol_column": source.symbol_column,
        "timestamp_column": source.timestamp_column,
        "signal_type_column": source.signal_type_column,
        "fixed_signal_type": source.fixed_signal_type,
        "price_column": source.price_column,
        "type_mapping": dict(source.type_mapping),
        "metadata_columns": dict(source.metadata_columns),
        "exchange": source.exchange,
        "exchange_column": source.exchange_column,
        "retain_all_columns": source.retain_all_columns,
        "timezone": str(source.source_timezone) if source.source_timezone else None,
    }
    fingerprint = hashlib.sha256(
        json.dumps(snapshot, sort_keys=True).encode()
    ).hexdigest()
    return source, SourceImportPreflight(
        source_id=source.source_id,
        display_name=source.display_name,
        database_path=source.database_path.resolve(),
        table=source.table_name,
        type_mapping=dict(source.type_mapping),
        exchange=source.exchange,
        metadata_columns=dict(source.metadata_columns),
        fingerprint=fingerprint,
    )


def preflight_single_source_import(
    configuration_path: Path,
    source_id: str,
    store: EventStore,
    *,
    allowed_roots: Sequence[Path | str] | None,
) -> SourceImportPreflight:
    """Validate a configured source without writing to source or destination."""
    if not isinstance(source_id, str) or not source_id.startswith("sqlite-"):
        raise ValueError("Only user-created SQLite sources are allowed")
    return _resolve_and_validate(configuration_path, source_id, store, allowed_roots)[1]


def import_single_source(
    configuration_path: Path,
    source_id: str,
    store: EventStore,
    *,
    allowed_roots: Sequence[Path | str] | None,
    lock_directory: Path,
    confirmed: bool,
    preflight_fingerprint: str,
    automatic: bool = False,
    progress_callback: Callable[[int], None] | None = None,
) -> SingleSourceImportResult:
    """Import one source after explicit confirmation and a fresh validation.

    All callers must use the same trusted lock directory. Lock files persist so
    concurrent processes never acquire locks on different inodes for one source.
    On failure counts are unavailable: the importer can have committed some rows.
    """
    if not automatic and confirmed is not True:
        return SingleSourceImportResult(error="Explicit confirmation is required")
    if not isinstance(source_id, str) or (
        not automatic and not source_id.startswith("sqlite-")
    ):
        return SingleSourceImportResult(
            error="Only user-created SQLite sources are allowed"
        )
    try:
        with source_import_lock(lock_directory, source_id, wait=False):
            source, checked = _resolve_and_validate(
                configuration_path, source_id, store, allowed_roots
            )
            if automatic and not source.automatic_sync_enabled:
                return SingleSourceImportResult(
                    error="Source synchronization is disabled"
                )
            if not automatic and (
                not preflight_fingerprint
                or checked.fingerprint != preflight_fingerprint
            ):
                raise ValueError("Source configuration changed; run preflight again")
            try:
                result = import_external_sqlite_events(
                    source,
                    store,
                    allowed_roots=allowed_roots,
                    progress_callback=progress_callback,
                )
            except Exception as exc:
                return SingleSourceImportResult(
                    error=(
                        f"Import failed; partial writes possible: "
                        f"{type(exc).__name__}: {exc}"
                    )
                )
            try:
                record_successful_source_import(store.database_path, source_id)
            except Exception as exc:
                return SingleSourceImportResult(
                    result.scanned,
                    result.inserted,
                    result.rejected,
                    error=f"Import completed but timestamp was not saved: {exc}",
                )
            return SingleSourceImportResult(
                result.scanned, result.inserted, result.rejected
            )
    except BlockingIOError:
        return SingleSourceImportResult(error="Source import is already running")
    except Exception as exc:
        return SingleSourceImportResult(error=f"{type(exc).__name__}: {exc}")
