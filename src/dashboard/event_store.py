"""Small SQLite store for final dashboard signal events."""

from __future__ import annotations

import json
import math
import sqlite3
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, TypeGuard

if TYPE_CHECKING:
    from dashboard.custom_signals import CustomSignalDefinition

DEFAULT_DATABASE_PATH = Path("data/dashboard/signal_events.sqlite3")
type FilterScalar = str | int | float | bool
CARD_LAYOUT_STANDARD_FIELD_NAMES = (
    "event_price",
    "exchange",
    "symbol",
    "primary_signal_id",
    "source_interval",
    "alert_level",
    "direction",
)
_FILTER_IDENTIFIER_PARTS = frozenset({"id", "identity", "key", "uuid", "code"})

_INSERT_EVENT_SQL = """
INSERT INTO signal_events (
    event_type, delivery_identity, exchange, symbol, event_at, event_price,
    primary_signal_id, pump_reference_signal_id, confirmation_signal_id,
    continuation_signal_id, reversal_identity, source_interval, alert_level,
    created_at, direction, metadata_json, owner_source_id
) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
ON CONFLICT(delivery_identity) DO NOTHING
"""


@dataclass(frozen=True, slots=True)
class SignalEvent:
    event_type: str
    delivery_identity: str
    exchange: str
    symbol: str
    event_at: str
    event_price: str | None
    primary_signal_id: str
    created_at: str
    source_interval: str | None = None
    alert_level: str | None = None
    direction: str | None = None
    pump_reference_signal_id: str | None = None
    confirmation_signal_id: str | None = None
    continuation_signal_id: str | None = None
    reversal_identity: str | None = None
    metadata_json: str = "{}"
    event_id: int | None = None
    owner_source_id: str | None = None


def _event_insert_values(event: SignalEvent) -> tuple[str | None, ...]:
    return (
        event.event_type,
        event.delivery_identity,
        event.exchange,
        event.symbol,
        event.event_at,
        event.event_price,
        event.primary_signal_id,
        event.pump_reference_signal_id,
        event.confirmation_signal_id,
        event.continuation_signal_id,
        event.reversal_identity,
        event.source_interval,
        event.alert_level,
        event.created_at,
        event.direction,
        event.metadata_json,
        event.owner_source_id,
    )


class EventStore:
    """Own the dashboard database only; production sources are never opened."""

    def __init__(
        self,
        database_path: Path | str = DEFAULT_DATABASE_PATH,
        *,
        allowed_event_types: Sequence[str] | Callable[[], Sequence[str]] = (
            "pump_reversal",
            "pump_pullback",
        ),
    ) -> None:
        self.database_path = Path(database_path)
        self._allowed_event_types = allowed_event_types
        if not self.allowed_event_types or len(self.allowed_event_types) != len(
            set(self.allowed_event_types)
        ):
            raise ValueError("allowed_event_types must be non-empty and unique")

    @property
    def allowed_event_types(self) -> tuple[str, ...]:
        values = self._allowed_event_types
        return tuple(values() if callable(values) else values)

    def initialize(self) -> None:
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            _initialize_schema(connection)

    def insert(self, event: SignalEvent) -> bool:
        return self.insert_many((event,)) == 1

    def insert_many(self, events: Sequence[SignalEvent]) -> int:
        """Insert one bounded event batch in a single destination transaction."""
        if not events:
            return 0
        for event in events:
            if event.event_type not in self.allowed_event_types:
                raise ValueError(f"Unknown event type: {event.event_type}")
            _validate_metadata_json(event.metadata_json)
        self.initialize()
        inserted = 0
        with self._connection() as connection:
            for event in events:
                cursor = connection.execute(
                    _INSERT_EVENT_SQL, _event_insert_values(event)
                )
                inserted += cursor.rowcount == 1
        return inserted

    def list_events(self, *, symbol: str | None = None) -> list[SignalEvent]:
        self._require_database()
        where = "" if symbol is None else " WHERE symbol = ?"
        parameters: tuple[str, ...] = () if symbol is None else (symbol,)
        with self._readonly_connection() as connection:
            columns = {
                row[1] for row in connection.execute("PRAGMA table_info(signal_events)")
            }
            metadata_column = (
                "metadata_json"
                if "metadata_json" in columns
                else "'{}' AS metadata_json"
            )
            owner_column = (
                "owner_source_id"
                if "owner_source_id" in columns
                else "NULL AS owner_source_id"
            )
            rows = connection.execute(
                "SELECT event_id, event_type, delivery_identity, exchange, symbol, "
                "event_at, "
                "event_price, primary_signal_id, created_at, "
                "pump_reference_signal_id, confirmation_signal_id, "
                "continuation_signal_id, reversal_identity, source_interval, "
                "alert_level, direction, "
                + metadata_column
                + ", "
                + owner_column
                + " FROM signal_events"
                + where
                + " ORDER BY event_at, event_id",
                parameters,
            ).fetchall()
        return [
            SignalEvent(
                **{
                    **dict(row),
                    "metadata_json": _safe_metadata_json(row["metadata_json"]),
                }
            )
            for row in rows
        ]

    def count_events(self, *, symbol: str | None = None) -> int:
        self._require_database()
        where = "" if symbol is None else " WHERE symbol = ?"
        parameters: tuple[str, ...] = () if symbol is None else (symbol,)
        with self._readonly_connection() as connection:
            return int(
                connection.execute(
                    "SELECT count(*) FROM signal_events" + where, parameters
                ).fetchone()[0]
            )

    def count_custom_signal_matches(
        self,
        definitions: Sequence[CustomSignalDefinition],
        *,
        batch_size: int = 500,
    ) -> dict[str, int]:
        """Count each definition's owned raw matches without live projection.

        This is deliberately independent from custom visibility and precedence:
        the Signal Types table reports imported events satisfying each saved
        definition, not the events currently rendered on the chart.
        """
        if not 1 <= batch_size <= 1_000:
            raise ValueError("batch_size must be between 1 and 1000")
        counts = {definition.signal_id: 0 for definition in definitions}
        if not definitions:
            return counts
        self._require_database()
        from dashboard.custom_signals import event_matches_custom_signal

        definitions_by_source: dict[str, list[CustomSignalDefinition]] = {}
        for definition in definitions:
            definitions_by_source.setdefault(definition.source_id, []).append(
                definition
            )
        with self._readonly_connection() as connection:
            columns = {
                row[1] for row in connection.execute("PRAGMA table_info(signal_events)")
            }
            if "owner_source_id" not in columns:
                return counts
            metadata_column = (
                "metadata_json"
                if "metadata_json" in columns
                else "'{}' AS metadata_json"
            )
            source_ids = tuple(definitions_by_source)
            rows = connection.execute(
                "SELECT event_id, event_type, delivery_identity, exchange, symbol, "
                "event_at, event_price, primary_signal_id, created_at, "
                "pump_reference_signal_id, confirmation_signal_id, "
                "continuation_signal_id, reversal_identity, source_interval, "
                "alert_level, direction, "
                + metadata_column
                + ", owner_source_id FROM signal_events WHERE owner_source_id IN ("
                + ", ".join("?" for _ in source_ids)
                + ")",
                source_ids,
            )
            while batch := rows.fetchmany(batch_size):
                for row in batch:
                    event = SignalEvent(
                        **{
                            **dict(row),
                            "metadata_json": _safe_metadata_json(row["metadata_json"]),
                        }
                    )
                    source_id = event.owner_source_id
                    if source_id is None:
                        continue
                    for definition in definitions_by_source[source_id]:
                        if event_matches_custom_signal(event, definition):
                            counts[definition.signal_id] += 1
        return counts

    def list_external_card_field_descriptors(
        self, source_id: str, event_type: str, *, metadata_limit: int = 200
    ) -> tuple[str, ...]:
        """Return bounded observed card fields for one owned external event set."""
        if not source_id or not event_type:
            raise ValueError("source_id and event_type are required")
        if not 1 <= metadata_limit <= 500:
            raise ValueError("metadata_limit must be between 1 and 500")
        self._require_database()
        with self._readonly_connection() as connection:
            columns = {
                str(row[1])
                for row in connection.execute("PRAGMA table_info(signal_events)")
            }
            if "owner_source_id" not in columns:
                return ()
            standard_names = tuple(
                name for name in CARD_LAYOUT_STANDARD_FIELD_NAMES if name in columns
            )
            standard: tuple[str, ...] = ()
            if standard_names:
                values = connection.execute(
                    "SELECT "
                    + ", ".join(
                        'MAX(CASE WHEN "'
                        + name
                        + '" IS NOT NULL THEN 1 ELSE 0 END) AS "'
                        + name
                        + '"'
                        for name in standard_names
                    )
                    + " FROM signal_events "
                    "WHERE owner_source_id = ? AND event_type = ?",
                    (source_id, event_type),
                ).fetchone()
                assert values is not None
                standard = tuple(
                    f"standard:{name}" for name in standard_names if values[name]
                )
            if "metadata_json" not in columns:
                return standard
            metadata = tuple(
                f"metadata:{row[0]}"
                for row in connection.execute(
                    "SELECT DISTINCT json_each.key "
                    "FROM signal_events JOIN json_each(signal_events.metadata_json) "
                    "WHERE owner_source_id = ? AND event_type = ? "
                    "AND json_valid(metadata_json) "
                    "ORDER BY json_each.key LIMIT ?",
                    (source_id, event_type, metadata_limit),
                )
            )
        return (*standard, *metadata)

    def list_owned_external_field_descriptors(
        self, source_id: str, *, metadata_limit: int = 200
    ) -> tuple[str, ...]:
        """Return bounded observed descriptors across all event types of one source."""
        if not source_id or not 1 <= metadata_limit <= 500:
            raise ValueError("Invalid source ID or metadata limit")
        self._require_database()
        with self._readonly_connection() as connection:
            columns = {
                str(row[1])
                for row in connection.execute("PRAGMA table_info(signal_events)")
            }
            if "owner_source_id" not in columns:
                return ()
            standard_names = tuple(
                name for name in CARD_LAYOUT_STANDARD_FIELD_NAMES if name in columns
            )
            standard: tuple[str, ...] = ()
            if standard_names:
                values = connection.execute(
                    "SELECT "
                    + ", ".join(
                        'MAX(CASE WHEN "'
                        + name
                        + '" IS NOT NULL THEN 1 ELSE 0 END) AS "'
                        + name
                        + '"'
                        for name in standard_names
                    )
                    + " FROM signal_events WHERE owner_source_id = ?",
                    (source_id,),
                ).fetchone()
                assert values is not None
                standard = tuple(
                    f"standard:{name}" for name in standard_names if values[name]
                )
            if "metadata_json" not in columns:
                return standard
            metadata = tuple(
                f"metadata:{row[0]}"
                for row in connection.execute(
                    "SELECT DISTINCT json_each.key FROM signal_events "
                    "JOIN json_each(signal_events.metadata_json) "
                    "WHERE owner_source_id = ? AND json_valid(metadata_json) "
                    "ORDER BY json_each.key LIMIT ?",
                    (source_id, metadata_limit),
                )
            )
        return (*standard, *metadata)

    def profile_owned_external_field(
        self,
        source_id: str,
        descriptor: str,
        *,
        sample_limit: int = 500,
        value_limit: int = 50,
    ) -> dict[str, object]:
        """Return bounded, source-owned filter choices without opening a source DB."""
        if (
            not source_id
            or not 1 <= sample_limit <= 1_000
            or not 1 <= value_limit <= 50
        ):
            raise ValueError("Invalid source ID or field profiling limit")
        namespace, separator, field_name = descriptor.partition(":")
        if (
            not separator
            or not field_name
            or "\x00" in field_name
            or (
                namespace == "standard"
                and field_name not in CARD_LAYOUT_STANDARD_FIELD_NAMES
            )
            or namespace not in {"standard", "metadata"}
        ):
            raise ValueError("Invalid custom signal condition field")
        self._require_database()
        with self._readonly_connection() as connection:
            columns = {
                str(row[1])
                for row in connection.execute("PRAGMA table_info(signal_events)")
            }
            if "owner_source_id" not in columns:
                return _field_profile(
                    descriptor, (), sampled=False, value_limit=value_limit
                )
            if namespace == "standard":
                if field_name not in columns:
                    return _field_profile(
                        descriptor, (), sampled=False, value_limit=value_limit
                    )
                rows = tuple(
                    connection.execute(
                        'SELECT "' + field_name + '" FROM signal_events '
                        'WHERE owner_source_id = ? AND "'
                        + field_name
                        + '" IS NOT NULL ORDER BY event_id DESC LIMIT ?',
                        (source_id, sample_limit),
                    )
                )
                values = tuple(row[0] for row in rows)
            else:
                rows = tuple(
                    connection.execute(
                        "SELECT metadata_json FROM signal_events "
                        "WHERE owner_source_id = ? AND json_valid(metadata_json) "
                        "ORDER BY event_id DESC LIMIT ?",
                        (source_id, sample_limit),
                    )
                )
                values = tuple(
                    metadata[field_name]
                    for row in rows
                    if isinstance(
                        metadata := json.loads(
                            _safe_metadata_json(row["metadata_json"])
                        ),
                        dict,
                    )
                    and field_name in metadata
                )
        return _field_profile(
            descriptor,
            values,
            sampled=len(rows) == sample_limit,
            value_limit=value_limit,
        )

    def list_external_card_field_samples(
        self, source_id: str, event_type: str, *, metadata_limit: int = 200
    ) -> dict[str, str | int | float]:
        """Return bounded latest non-null observed values for card fields."""
        fields = self.list_external_card_field_descriptors(
            source_id, event_type, metadata_limit=metadata_limit
        )
        if not fields:
            return {}
        with self._readonly_connection() as connection:
            samples: dict[str, str | int | float] = {}
            for descriptor in fields:
                namespace, field_name = descriptor.split(":", 1)
                if namespace == "standard":
                    value = connection.execute(
                        'SELECT "'
                        + field_name
                        + '" FROM signal_events WHERE owner_source_id = ? '
                        'AND event_type = ? AND "'
                        + field_name
                        + '" IS NOT NULL ORDER BY event_id DESC LIMIT 1',
                        (source_id, event_type),
                    ).fetchone()[0]
                else:
                    row = connection.execute(
                        "SELECT json_each.value FROM signal_events "
                        "JOIN json_each(signal_events.metadata_json) "
                        "WHERE owner_source_id = ? AND event_type = ? "
                        "AND json_each.key = ? AND json_each.value IS NOT NULL "
                        "ORDER BY event_id DESC LIMIT 1",
                        (source_id, event_type, field_name),
                    ).fetchone()
                    value = None if row is None else row[0]
                if isinstance(value, (str, int, float)):
                    samples[descriptor] = value
        return samples

    def get_external_card_preview_event(
        self, source_id: str, event_type: str
    ) -> dict[str, object] | None:
        """Return one latest owned event and its fields for a coherent card preview."""
        if not source_id or not event_type:
            raise ValueError("source_id and event_type are required")
        self._require_database()
        with self._readonly_connection() as connection:
            columns = {
                str(row[1])
                for row in connection.execute("PRAGMA table_info(signal_events)")
            }
            if "owner_source_id" not in columns:
                return None
            standard_names = tuple(
                name for name in CARD_LAYOUT_STANDARD_FIELD_NAMES if name in columns
            )
            metadata_column = (
                "metadata_json"
                if "metadata_json" in columns
                else "'{}' AS metadata_json"
            )
            select_columns = [
                "event_at",
                *('"' + name + '"' for name in standard_names),
                metadata_column,
            ]
            row = connection.execute(
                "SELECT "
                + ", ".join(select_columns)
                + " FROM signal_events WHERE owner_source_id = ? AND event_type = ? "
                "ORDER BY event_id DESC LIMIT 1",
                (source_id, event_type),
            ).fetchone()
        if row is None:
            return None
        metadata = json.loads(_safe_metadata_json(row["metadata_json"]))
        assert isinstance(metadata, dict)
        fields: dict[str, object] = {
            f"standard:{name}": row[name] for name in standard_names
        }
        fields.update({f"metadata:{name}": value for name, value in metadata.items()})
        return {"event_at": str(row["event_at"]), "fields": fields}

    def custom_card_context(
        self,
        definition: CustomSignalDefinition,
    ) -> tuple[tuple[str, ...], dict[str, str | int | float], dict[str, object] | None]:
        """Discover fields from source events matching one definition for preview."""
        from dashboard.custom_signals import event_matches_custom_signal

        matches = []
        for event in self.list_events():
            if event.owner_source_id != definition.source_id:
                continue
            if event_matches_custom_signal(event, definition):
                matches.append(event)
        if not matches:
            return (), {}, None
        latest = max(matches, key=lambda item: (item.event_at, item.event_id or 0))
        descriptors: set[str] = set()
        for event in matches:
            for name in CARD_LAYOUT_STANDARD_FIELD_NAMES:
                if getattr(event, name) is not None:
                    descriptors.add(f"standard:{name}")
            metadata = json.loads(_safe_metadata_json(event.metadata_json))
            descriptors.update(f"metadata:{name}" for name in metadata)
        fields = self._card_fields(latest)
        samples = {
            descriptor: value
            for descriptor, value in fields.items()
            if descriptor in descriptors and isinstance(value, (str, int, float))
        }
        return (
            tuple(sorted(descriptors)),
            samples,
            {
                "event_at": latest.event_at,
                "fields": fields,
            },
        )

    def _card_fields(self, event: SignalEvent) -> dict[str, object]:
        metadata = json.loads(_safe_metadata_json(event.metadata_json))
        fields = {
            f"standard:{name}": getattr(event, name)
            for name in CARD_LAYOUT_STANDARD_FIELD_NAMES
        }
        fields.update({f"metadata:{name}": value for name, value in metadata.items()})
        return fields

    def list_owned_external_sources_for_event_type(
        self, event_type: str, *, limit: int = 2
    ) -> tuple[str, ...]:
        """Return a bounded owner set for resolving a source-scoped editor."""
        if not event_type or not 1 <= limit <= 50:
            raise ValueError("Invalid event type or source limit")
        self._require_database()
        with self._readonly_connection() as connection:
            columns = {
                str(row[1])
                for row in connection.execute("PRAGMA table_info(signal_events)")
            }
            if "owner_source_id" not in columns:
                return ()
            return tuple(
                str(row[0])
                for row in connection.execute(
                    "SELECT DISTINCT owner_source_id FROM signal_events "
                    "WHERE event_type = ? AND owner_source_id IS NOT NULL "
                    "ORDER BY owner_source_id LIMIT ?",
                    (event_type, limit),
                )
            )

    def import_cursor(self, name: str) -> tuple[str, str] | None:
        """Return the durable source cursor for a development-only importer."""
        self.initialize()
        with self._readonly_connection() as connection:
            row = connection.execute(
                "SELECT detected_at, signal_id FROM dashboard_import_cursors "
                "WHERE name = ?",
                (name,),
            ).fetchone()
        return None if row is None else (str(row["detected_at"]), str(row["signal_id"]))

    def set_import_cursor(self, name: str, detected_at: str, signal_id: str) -> None:
        """Advance a development-only import cursor after successful inserts."""
        self.initialize()
        with self._connection() as connection:
            connection.execute(
                """
                INSERT INTO dashboard_import_cursors(name, detected_at, signal_id)
                VALUES (?, ?, ?)
                ON CONFLICT(name) DO UPDATE SET
                    detected_at = excluded.detected_at,
                    signal_id = excluded.signal_id
                """,
                (name, detected_at, signal_id),
            )

    def import_event_id_cursor(self, name: str) -> int | None:
        """Return a source event-ID high-water mark for a development importer."""
        self.initialize()
        with self._readonly_connection() as connection:
            row = connection.execute(
                "SELECT source_event_id FROM dashboard_import_event_cursors "
                "WHERE name = ?",
                (name,),
            ).fetchone()
        return None if row is None else int(row["source_event_id"])

    def set_import_event_id_cursor(self, name: str, source_event_id: int) -> None:
        """Advance a source event-ID high-water mark after a successful import."""
        self.initialize()
        with self._connection() as connection:
            connection.execute(
                """
                INSERT INTO dashboard_import_event_cursors(name, source_event_id)
                VALUES (?, ?)
                ON CONFLICT(name) DO UPDATE SET
                    source_event_id = excluded.source_event_id
                """,
                (name, source_event_id),
            )

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.database_path)
        try:
            yield connection
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    @contextmanager
    def _readonly_connection(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(
            f"file:{self.database_path.absolute()}?mode=ro", uri=True
        )
        connection.row_factory = sqlite3.Row
        try:
            connection.execute("PRAGMA query_only = ON")
            yield connection
        finally:
            connection.close()

    def _require_database(self) -> None:
        if not self.database_path.exists():
            raise FileNotFoundError(self.database_path)


_EVENT_COLUMNS = (
    "event_id",
    "event_type",
    "delivery_identity",
    "exchange",
    "symbol",
    "event_at",
    "event_price",
    "primary_signal_id",
    "pump_reference_signal_id",
    "confirmation_signal_id",
    "continuation_signal_id",
    "reversal_identity",
    "source_interval",
    "alert_level",
    "created_at",
    "direction",
    "metadata_json",
)

_LEGACY_EVENT_COLUMNS = _EVENT_COLUMNS[:-1]


def _initialize_schema(connection: sqlite3.Connection) -> None:
    table_sql = connection.execute(
        "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'signal_events'"
    ).fetchone()
    if table_sql is not None and "CHECK(EVENT_TYPE" in str(
        table_sql[0]
    ).upper().replace(" ", ""):
        _migrate_event_type_allowlist(connection)
    connection.execute(_signal_events_schema("IF NOT EXISTS "))
    _add_metadata_column_if_missing(connection)
    _add_owner_column_if_missing(connection)
    connection.execute(_cursor_schema())
    connection.execute(_event_id_cursor_schema())


def _migrate_event_type_allowlist(connection: sqlite3.Connection) -> None:
    columns = tuple(
        row[1] for row in connection.execute("PRAGMA table_info(signal_events)")
    )
    if columns not in (
        _LEGACY_EVENT_COLUMNS,
        _EVENT_COLUMNS,
        _EVENT_COLUMNS + ("owner_source_id",),
    ):
        raise RuntimeError("Unexpected signal_events schema; refusing migration")
    indexes = [
        str(row[0])
        for row in connection.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'index' "
            "AND tbl_name = 'signal_events' "
            "AND sql IS NOT NULL"
        )
    ]
    connection.execute("SAVEPOINT migrate_event_type_allowlist")
    try:
        connection.execute(_signal_events_schema("", table_name="signal_events_new"))
        if "owner_source_id" in columns:
            connection.execute(
                "ALTER TABLE signal_events_new ADD COLUMN owner_source_id TEXT"
            )
        names = ", ".join(columns)
        connection.execute(
            f"INSERT INTO signal_events_new ({names}) SELECT {names} FROM signal_events"
        )
        connection.execute("DROP TABLE signal_events")
        connection.execute("ALTER TABLE signal_events_new RENAME TO signal_events")
        for index_sql in indexes:
            connection.execute(index_sql)
    except BaseException:
        connection.execute("ROLLBACK TO migrate_event_type_allowlist")
        connection.execute("RELEASE migrate_event_type_allowlist")
        raise
    connection.execute("RELEASE migrate_event_type_allowlist")


def _add_metadata_column_if_missing(connection: sqlite3.Connection) -> None:
    columns = {row[1] for row in connection.execute("PRAGMA table_info(signal_events)")}
    if "metadata_json" not in columns:
        connection.execute(
            "ALTER TABLE signal_events "
            "ADD COLUMN metadata_json TEXT NOT NULL DEFAULT '{}'"
        )


def _add_owner_column_if_missing(connection: sqlite3.Connection) -> None:
    columns = {row[1] for row in connection.execute("PRAGMA table_info(signal_events)")}
    if "owner_source_id" not in columns:
        connection.execute("ALTER TABLE signal_events ADD COLUMN owner_source_id TEXT")


def _signal_events_schema(
    if_not_exists: str, *, table_name: str = "signal_events"
) -> str:
    return f"""
CREATE TABLE {if_not_exists}{table_name} (
    event_id INTEGER PRIMARY KEY,
    event_type TEXT NOT NULL,
    delivery_identity TEXT NOT NULL UNIQUE,
    exchange TEXT NOT NULL,
    symbol TEXT NOT NULL,
    event_at TEXT NOT NULL,
    event_price TEXT,
    primary_signal_id TEXT NOT NULL,
    pump_reference_signal_id TEXT,
    confirmation_signal_id TEXT,
    continuation_signal_id TEXT,
    reversal_identity TEXT,
    source_interval TEXT,
    alert_level TEXT,
    created_at TEXT NOT NULL,
    direction TEXT,
    metadata_json TEXT NOT NULL DEFAULT '{{}}'
);
"""


def _finite_json_float(value: str) -> float:
    result = float(value)
    if not math.isfinite(result):
        raise ValueError("Non-finite JSON number")
    return result


def _reject_json_constant(value: str) -> None:
    raise ValueError(f"Invalid JSON constant: {value}")


def _validate_metadata_json(value: str) -> None:
    try:
        parsed = json.loads(
            value, parse_float=_finite_json_float, parse_constant=_reject_json_constant
        )
    except (TypeError, ValueError) as exc:
        raise ValueError(
            "metadata_json must be a JSON object with finite numbers"
        ) from exc
    if not isinstance(parsed, dict):
        raise ValueError("metadata_json must be a JSON object with finite numbers")


def _safe_metadata_json(value: object) -> str:
    if not isinstance(value, str):
        return "{}"
    try:
        _validate_metadata_json(value)
    except ValueError:
        return "{}"
    return value


def _field_profile(
    descriptor: str,
    values: Sequence[object],
    *,
    sampled: bool,
    value_limit: int,
) -> dict[str, object]:
    scalars = tuple(value for value in values if _filter_scalar(value))
    numeric = _profile_numbers(descriptor, scalars)
    if numeric is not None:
        return {
            "field": descriptor,
            "kind": "numeric",
            "min": min(numeric),
            "max": max(numeric),
            "sampled": sampled,
        }
    unique: dict[tuple[str, str], FilterScalar] = {}
    for value in scalars:
        key = (type(value).__name__, json.dumps(value, sort_keys=True))
        unique.setdefault(key, value)
    choices = tuple(unique.values())
    return {
        "field": descriptor,
        "kind": "categorical",
        "values": list(choices[:value_limit]),
        "truncated": len(choices) > value_limit,
        "sampled": sampled,
    }


def _filter_scalar(value: object) -> TypeGuard[FilterScalar]:
    return isinstance(value, (str, int, float, bool)) and not (
        isinstance(value, float) and not math.isfinite(value)
    )


def _profile_numbers(
    descriptor: str, values: Sequence[FilterScalar]
) -> tuple[float, ...] | None:
    field_name = descriptor.split(":", 1)[1].casefold()
    if any(part in _FILTER_IDENTIFIER_PARTS for part in field_name.split("_")):
        return None
    if len({_profile_identity(value) for value in values}) <= 10:
        return None
    numbers: list[float] = []
    for value in values:
        if isinstance(value, bool):
            return None
        try:
            number = float(value)
        except (TypeError, ValueError):
            return None
        if not math.isfinite(number):
            return None
        numbers.append(number)
    return tuple(numbers) if numbers else None


def _profile_identity(value: object) -> tuple[str, str]:
    return type(value).__name__, json.dumps(value, sort_keys=True)


def _cursor_schema() -> str:
    return """
CREATE TABLE IF NOT EXISTS dashboard_import_cursors (
    name TEXT PRIMARY KEY,
    detected_at TEXT NOT NULL,
    signal_id TEXT NOT NULL
);
"""


def _event_id_cursor_schema() -> str:
    return """
CREATE TABLE IF NOT EXISTS dashboard_import_event_cursors (
    name TEXT PRIMARY KEY,
    source_event_id INTEGER NOT NULL
);
"""
