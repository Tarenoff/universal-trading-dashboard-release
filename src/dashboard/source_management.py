"""Reusable source-management wiring for dashboard application factories."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from pathlib import Path

from fastapi import FastAPI

from dashboard.event_store import EventStore
from dashboard.external_sqlite_import import ExternalSqliteSource
from dashboard.signal_type_registry import SignalTypeRegistry
from dashboard.source_settings import add_signal_source_settings_routes


def attach_source_management(
    app: FastAPI,
    *,
    events_path: Path,
    configuration_path: Path | None,
    allowed_database_roots: Sequence[Path | str] | None,
    registry_provider: Callable[[], SignalTypeRegistry],
    sources: Sequence[ExternalSqliteSource] = (),
) -> None:
    """Attach the shared SQLite source lifecycle to one isolated dashboard."""
    app.state.signal_sources_navigation = True
    add_signal_source_settings_routes(
        app,
        sources=sources,
        registered_signal_types=(),
        signal_types_provider=lambda: registry_provider().signal_types,
        allowed_database_roots=allowed_database_roots,
        configuration_path=configuration_path,
        import_store=EventStore(
            events_path,
            allowed_event_types=lambda: tuple(
                item.event_type for item in registry_provider().signal_types
            ),
        ),
        import_lock_directory=events_path.parent / ".single-source-import-locks",
    )
