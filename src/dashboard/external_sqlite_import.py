"""Configuration-driven, read-only import from a local SQLite signal source."""

from __future__ import annotations

import json
import math
import sqlite3
import time
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from types import MappingProxyType
from zoneinfo import ZoneInfo

from dashboard.event_store import (
    CARD_LAYOUT_STANDARD_FIELD_NAMES,
    EventStore,
    SignalEvent,
)

_PREVIEW_TIMEOUT_SECONDS = 10.0
_PREVIEW_PROGRESS_STEPS = 1_000
_PREVIEW_CANDIDATE_MULTIPLIER = 50
_IMPORT_BATCH_SIZE = 500
CARD_LAYOUT_STANDARD_FIELDS = frozenset(CARD_LAYOUT_STANDARD_FIELD_NAMES)


@dataclass(frozen=True, slots=True)
class SignalCardLayout:
    """Persistent card field selection for one source and destination type."""

    compact_fields: tuple[str, ...]
    detail_fields: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ExternalSqliteSource:
    source_id: str
    database_path: Path
    table_name: str
    primary_key_column: str
    symbol_column: str
    timestamp_column: str
    signal_type_column: str | None
    allowed_source_types: frozenset[str]
    type_mapping: Mapping[str, str]
    price_column: str | None = None
    display_name: str = ""
    source_timezone: ZoneInfo | None = None
    sync_interval_seconds: int = 30
    exchange: str = "BYBIT"
    enabled: bool = True
    sync_enabled: bool = False
    fixed_signal_type: str | None = None
    metadata_columns: Mapping[str, str] = field(default_factory=dict)
    retain_all_columns: bool = False
    exchange_column: str | None = None
    visibility_enabled: bool | None = None
    source_origin: str | None = None
    pending_removal: bool = False
    card_layouts: Mapping[str, SignalCardLayout] = field(default_factory=dict)

    @property
    def is_visible(self) -> bool:
        return (
            self.enabled if self.visibility_enabled is None else self.visibility_enabled
        )

    @property
    def automatic_sync_enabled(self) -> bool:
        if self.pending_removal:
            return False
        if self.visibility_enabled is None:
            return self.enabled and self.sync_enabled
        return self.sync_enabled

    def __post_init__(self) -> None:
        if not self.source_id or not self.allowed_source_types:
            raise ValueError("source_id and allowed_source_types are required")
        if (self.signal_type_column is None) == (self.fixed_signal_type is None):
            raise ValueError("exactly one signal type mapping is required")
        if self.sync_interval_seconds <= 0:
            raise ValueError("sync_interval_seconds must be positive")
        if not isinstance(self.enabled, bool):
            raise ValueError("enabled must be a boolean")
        if not isinstance(self.sync_enabled, bool):
            raise ValueError("sync_enabled must be a boolean")
        if not isinstance(self.pending_removal, bool):
            raise ValueError("pending_removal must be a boolean")
        if self.visibility_enabled is not None and not isinstance(
            self.visibility_enabled, bool
        ):
            raise ValueError("visibility_enabled must be a boolean")
        if not all(
            isinstance(name, str) and isinstance(column, str)
            for name, column in self.metadata_columns.items()
        ):
            raise ValueError("metadata_columns must be a string mapping")
        _validate_identifiers(
            self.table_name,
            self.primary_key_column,
            self.symbol_column,
            self.timestamp_column,
            self.signal_type_column,
            self.price_column,
            self.exchange_column,
            *self.metadata_columns.keys(),
            *self.metadata_columns.values(),
        )
        if not set(self.allowed_source_types) <= set(self.type_mapping):
            raise ValueError("type_mapping must cover allowed_source_types")
        if not all(
            isinstance(type_id, str) and isinstance(layout, SignalCardLayout)
            for type_id, layout in self.card_layouts.items()
        ):
            raise ValueError("card_layouts must map signal type IDs to card layouts")
        object.__setattr__(
            self, "type_mapping", MappingProxyType(dict(self.type_mapping))
        )
        object.__setattr__(
            self, "metadata_columns", MappingProxyType(dict(self.metadata_columns))
        )
        object.__setattr__(
            self, "card_layouts", MappingProxyType(dict(self.card_layouts))
        )


@dataclass(frozen=True, slots=True)
class ExternalImportResult:
    scanned: int
    inserted: int
    rejected: int


def inspect_external_sqlite_tables(
    database_path: Path | str,
    *,
    allowed_roots: Sequence[Path | str] | None = None,
) -> list[str]:
    """List user tables through the same read-only SQLite protections as import."""
    try:
        with _readonly_source_connection(database_path, allowed_roots) as connection:
            rows = connection.execute(
                "SELECT name FROM sqlite_master "
                "WHERE type = 'table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
            ).fetchall()
    except sqlite3.Error as exc:
        raise ValueError("Could not inspect SQLite database") from exc
    return [str(row[0]) for row in rows]


def inspect_external_sqlite_columns(
    database_path: Path | str,
    table_name: str,
    *,
    allowed_roots: Sequence[Path | str] | None = None,
) -> list[dict[str, str]]:
    """List selected-table columns without accepting browser-provided SQL."""
    _validate_identifiers(table_name)
    try:
        with _readonly_source_connection(database_path, allowed_roots) as connection:
            rows = connection.execute(
                f"PRAGMA table_info({_quote(table_name)})"
            ).fetchall()
    except sqlite3.Error as exc:
        raise ValueError("Could not inspect SQLite database") from exc
    return [{"name": str(row[1]), "type": str(row[2]) or "—"} for row in rows]


def preview_external_sqlite_rows(
    database_path: Path | str,
    table_name: str,
    column_names: Sequence[str],
    *,
    allowed_roots: Sequence[Path | str] | None = None,
    limit: int = 20,
) -> list[dict[str, object]]:
    """Read a bounded, identifier-validated source preview without importing."""
    if not 1 <= limit <= 20:
        raise ValueError("Preview limit must be between 1 and 20")
    if not column_names:
        raise ValueError("Preview requires at least one source column")
    _validate_identifiers(table_name, *column_names)
    try:
        with _readonly_source_connection(database_path, allowed_roots) as connection:
            query = (
                "SELECT "
                + ", ".join(map(_quote, column_names))
                + f" FROM {_quote(table_name)} LIMIT ?"
            )
            rows = connection.execute(query, (limit,)).fetchall()
    except sqlite3.Error as exc:
        raise ValueError("Could not preview SQLite source rows") from exc
    return [dict(row) for row in rows]


def preview_matching_external_sqlite_rows(
    database_path: Path | str,
    table_name: str,
    column_names: Sequence[str],
    *,
    allowed_roots: Sequence[Path | str] | None,
    signal_type_column: str | None,
    selected_types: Sequence[str],
    limit: int = 10,
) -> list[dict[str, object]]:
    """Read selected examples from a bounded source-row sample."""
    if not 1 <= limit <= 10 or not column_names:
        raise ValueError("Invalid preview columns or limit")
    _validate_identifiers(table_name, signal_type_column, *column_names)
    if signal_type_column is not None and not selected_types:
        raise ValueError("At least one signal type is required")
    timed_out = False
    try:
        with _readonly_source_connection(database_path, allowed_roots) as connection:
            candidate_limit = (
                limit
                if signal_type_column is None
                else limit * _PREVIEW_CANDIDATE_MULTIPLIER
            )
            deadline = time.monotonic() + _PREVIEW_TIMEOUT_SECONDS

            def interrupt_if_timed_out() -> int:
                nonlocal timed_out
                timed_out = time.monotonic() >= deadline
                return int(timed_out)

            connection.set_progress_handler(
                interrupt_if_timed_out, _PREVIEW_PROGRESS_STEPS
            )
            try:
                query = (
                    "SELECT "
                    + ", ".join(map(_quote, column_names))
                    + f" FROM {_quote(table_name)} LIMIT ?"
                )
                rows = connection.execute(query, (candidate_limit,)).fetchall()
            finally:
                connection.set_progress_handler(None, 0)
    except sqlite3.Error as exc:
        if timed_out:
            raise ValueError(
                "Read-only preview timed out after 10 seconds. "
                "Check that the source database is available and try again."
            ) from exc
        raise ValueError("Could not preview SQLite source rows") from exc
    normalized_rows = [dict(row) for row in rows]
    if signal_type_column is None:
        return normalized_rows
    selected = set(selected_types)
    return [
        row
        for row in normalized_rows
        if _preview_type_text(row[signal_type_column]) in selected
    ][:limit]


def _preview_type_text(value: object) -> str | None:
    """Match the importer conversion without putting a Python UDF in SQL."""
    return None if value is None or isinstance(value, bytes) else str(value)


def import_external_sqlite_events(
    source: ExternalSqliteSource,
    destination: EventStore,
    *,
    allowed_roots: Sequence[Path | str] | None = None,
    progress_callback: Callable[[int], None] | None = None,
) -> ExternalImportResult:
    """Import selected source rows, rejecting malformed records explicitly.

    ``progress_callback`` receives the number of source rows processed so far.
    It does not require a source-row total and includes rejected or unselected rows.
    """
    unknown_types = set(source.type_mapping.values()) - set(
        destination.allowed_event_types
    )
    if unknown_types:
        raise ValueError(f"Unknown destination event type: {sorted(unknown_types)[0]}")
    columns = [
        source.primary_key_column,
        source.symbol_column,
        source.timestamp_column,
    ]
    if source.signal_type_column is not None:
        columns.append(source.signal_type_column)
    if source.price_column is not None:
        columns.append(source.price_column)
    if source.exchange_column is not None:
        columns.append(source.exchange_column)
    if source.retain_all_columns:
        columns.extend(
            item["name"]
            for item in inspect_external_sqlite_columns(
                source.database_path, source.table_name, allowed_roots=allowed_roots
            )
        )
    else:
        columns.extend(source.metadata_columns.values())
    columns = list(dict.fromkeys(columns))
    query = (
        "SELECT "
        + ", ".join(map(_quote, columns))
        + f" FROM {_quote(source.table_name)}"
    )
    parameters: tuple[str, ...] = ()
    if source.signal_type_column is not None:
        # Keep the comparison case-sensitive like the existing Python mapping.
        # Sources with a default BINARY-collated type column can use its index.
        placeholders = ", ".join("?" for _ in source.allowed_source_types)
        query += (
            f" WHERE {_quote(source.signal_type_column)} COLLATE BINARY "
            f"IN ({placeholders})"
        )
        parameters = tuple(sorted(source.allowed_source_types))
    scanned = inserted = rejected = 0
    with _readonly_source_connection(source.database_path, allowed_roots) as conn:
        cursor = conn.execute(query, parameters)
        while rows := cursor.fetchmany(_IMPORT_BATCH_SIZE):
            events: list[SignalEvent] = []
            for row in rows:
                scanned += 1
                try:
                    source_type = source.fixed_signal_type or _required(
                        row[source.signal_type_column], "signal type"
                    )
                    if source_type not in source.allowed_source_types:
                        continue
                    timestamp = _utc_timestamp(
                        _required(row[source.timestamp_column], "timestamp"),
                        source.source_timezone,
                    )
                    primary_key = _required(
                        row[source.primary_key_column], "primary key"
                    )
                    symbol = _required(row[source.symbol_column], "symbol")
                    price = (
                        None
                        if source.price_column is None
                        else row[source.price_column]
                    )
                    events.append(
                        SignalEvent(
                            event_type=source.type_mapping[source_type],
                            delivery_identity=f"external:{source.source_id}:{primary_key}",
                            owner_source_id=source.source_id,
                            exchange=(
                                str(row[source.exchange_column])
                                if row[source.exchange_column] is not None
                                else ""
                            )
                            if source.exchange_column
                            else source.exchange,
                            symbol=symbol,
                            event_at=timestamp,
                            event_price=None if price is None else str(price),
                            primary_signal_id=primary_key,
                            created_at=timestamp,
                            metadata_json=_metadata_json(
                                row,
                                {name: name for name in columns}
                                if source.retain_all_columns
                                else source.metadata_columns,
                                strict=source.retain_all_columns,
                            ),
                        )
                    )
                except (KeyError, TypeError, ValueError):
                    rejected += 1
            inserted += destination.insert_many(events)
            if progress_callback is not None:
                for processed in range(scanned - len(rows) + 1, scanned + 1):
                    progress_callback(processed)
    return ExternalImportResult(scanned=scanned, inserted=inserted, rejected=rejected)


def _metadata_json(
    row: sqlite3.Row | Mapping[str, object],
    columns: Mapping[str, str],
    *,
    strict: bool = False,
) -> str:
    metadata: dict[str, str | int | float | None] = {}
    for name, column in columns.items():
        value = row[column]
        if value is None:
            if strict:
                metadata[name] = None
            continue
        if isinstance(value, bytes):
            if strict:
                raise UnsupportedMetadataError(
                    f"Unsupported SQLite BLOB in column {name}"
                )
            continue
        if isinstance(value, float) and not math.isfinite(value):
            if strict:
                raise UnsupportedMetadataError(
                    f"Unsupported non-finite number in column {name}"
                )
            continue
        if isinstance(value, (str, int, float)):
            metadata[name] = value
    return json.dumps(metadata, separators=(",", ":"), ensure_ascii=False)


class UnsupportedMetadataError(Exception):
    """A source value cannot be represented in JSON metadata."""


def _validate_identifiers(*identifiers: str | None) -> None:
    if any(
        value is not None and (not value or "\x00" in value) for value in identifiers
    ):
        raise ValueError("Invalid SQLite identifier in source configuration")


def _quote(identifier: str) -> str:
    _validate_identifiers(identifier)
    return '"' + identifier.replace('"', '""') + '"'


def distinct_external_sqlite_values(
    database_path: Path | str,
    table_name: str,
    column: str,
    *,
    allowed_roots: Sequence[Path | str] | None = None,
) -> list[str]:
    with _readonly_source_connection(database_path, allowed_roots) as connection:
        try:
            rows = connection.execute(
                f"SELECT DISTINCT {_quote(column)} COLLATE BINARY, "
                f"typeof({_quote(column)}) "
                f"FROM {_quote(table_name)}"
            ).fetchall()
        except sqlite3.Error as exc:
            raise ValueError("Could not discover signal type values") from exc
    values = []
    for row in rows:
        value = row[0]
        if value is None:
            continue
        if isinstance(value, bytes):
            raise ValueError("Unsupported SQLite BLOB in signal type column")
        text = str(value)
        if text.strip() and text not in values:
            values.append(text)
    return sorted(values)


@contextmanager
def _readonly_source_connection(
    database_path: Path | str,
    allowed_roots: Sequence[Path | str] | None = None,
) -> Iterator[sqlite3.Connection]:
    requested_path = Path(database_path)
    if not requested_path.is_absolute():
        raise ValueError("SQLite database path must be absolute")
    try:
        path = requested_path.resolve()
        exists = path.exists()
        is_file = path.is_file()
    except OSError as exc:
        raise ValueError(f"Could not access SQLite database path: {exc}") from exc
    if allowed_roots is not None:
        roots = tuple(Path(root).resolve() for root in allowed_roots)
        if not roots or not any(_is_within(path, root) for root in roots):
            raise ValueError("SQLite database path is not allowed")
    if not exists:
        raise ValueError("SQLite database file was not found")
    if not is_file:
        raise ValueError("SQLite database path is not a regular file")
    try:
        connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA query_only = ON")
        connection.execute("PRAGMA schema_version").fetchone()
    except sqlite3.Error as exc:
        raise ValueError(f"Could not open SQLite database read-only: {exc}") from exc
    try:
        yield connection
    finally:
        connection.close()


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def _required(value: object, field: str) -> str:
    if value is None or not str(value).strip():
        raise ValueError(f"Missing {field}")
    return str(value)


def _utc_timestamp(value: str, source_timezone: ZoneInfo | None = None) -> str:
    normalized = value.replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError as exc:
        raise ValueError("Invalid timestamp") from exc
    if parsed.tzinfo is None:
        if source_timezone is None:
            raise ValueError("Timestamp must include a timezone")
        first = parsed.replace(tzinfo=source_timezone, fold=0)
        second = parsed.replace(tzinfo=source_timezone, fold=1)
        if first.utcoffset() != second.utcoffset():
            raise ValueError("Ambiguous or nonexistent local timestamp")
        parsed = first
    return parsed.astimezone(UTC).isoformat()
