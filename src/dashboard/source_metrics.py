"""Read-only source counters and successful import timestamps."""

from __future__ import annotations

import sqlite3
from collections.abc import Sequence
from contextlib import closing
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path


@dataclass(frozen=True, slots=True)
class SourceMetrics:
    events: int
    last_import: str | None


def read_source_metrics(
    path: Path, source_ids: Sequence[str]
) -> dict[str, SourceMetrics]:
    """Count identities attributed to each source, including old unowned imports."""
    if not path.exists():
        return {source_id: SourceMetrics(0, None) for source_id in source_ids}
    uri = path.resolve().as_uri() + "?mode=ro"
    with closing(sqlite3.connect(uri, uri=True)) as connection:
        columns = {
            row[1] for row in connection.execute("PRAGMA table_info(signal_events)")
        }
        if columns and not {"delivery_identity", "primary_signal_id"} <= columns:
            raise ValueError("Event identity is unavailable")
        owner_clause = (
            " AND (owner_source_id IS NULL OR owner_source_id = ?)"
            if "owner_source_id" in columns
            else ""
        )
        has_imports = (
            connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' "
                "AND name = 'dashboard_source_imports'"
            ).fetchone()
            is not None
        )
        timestamps = (
            dict(
                connection.execute(
                    "SELECT source_id, last_success_at FROM dashboard_source_imports"
                )
            )
            if has_imports
            else {}
        )
        metrics = {}
        for source_id in source_ids:
            if not columns:
                metrics[source_id] = SourceMetrics(0, timestamps.get(source_id))
                continue
            prefix = f"external:{source_id}:"
            # ':' is the final delimiter; ';' is its next binary code point.
            parameters: tuple[str, ...] = (prefix, prefix[:-1] + ";", prefix)
            if owner_clause:
                parameters += (source_id,)
            row = connection.execute(
                "SELECT count(*) FROM signal_events WHERE delivery_identity >= ? "
                "AND delivery_identity < ? AND delivery_identity = ? "
                "|| primary_signal_id" + owner_clause,
                parameters,
            ).fetchone()
            metrics[source_id] = SourceMetrics(int(row[0]), timestamps.get(source_id))
        return metrics


def record_successful_source_import(path: Path, source_id: str) -> None:
    """Record completion time only after a source import returns successfully."""
    timestamp = datetime.now(UTC).isoformat(timespec="seconds")
    path.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(path)) as connection, connection:
        connection.execute(
            "CREATE TABLE IF NOT EXISTS dashboard_source_imports "
            "(source_id TEXT PRIMARY KEY, last_success_at TEXT NOT NULL)"
        )
        connection.execute(
            "INSERT INTO dashboard_source_imports(source_id, last_success_at) "
            "VALUES (?, ?) ON CONFLICT(source_id) DO UPDATE "
            "SET last_success_at = excluded.last_success_at",
            (source_id, timestamp),
        )
