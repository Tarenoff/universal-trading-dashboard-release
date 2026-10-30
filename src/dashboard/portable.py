"""Portable dashboard factory without infrastructure-specific inputs."""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

from fastapi import FastAPI

from dashboard.app import create_app
from dashboard.custom_signals import CustomSignalDefinition
from dashboard.event_store import EventStore
from dashboard.external_source_config import (
    ExternalSourcesConfiguration,
    load_external_sources_configuration,
    registry_with_display_overrides,
)
from dashboard.market_data_config import (
    MarketDataConfiguration,
    validate_local_market_data,
)
from dashboard.signal_type_registry import (
    PRODUCTION_SIGNAL_TYPE_REGISTRY,
    SignalTypeRegistry,
)
from dashboard.source_management import attach_source_management
from dashboard.source_settings import add_signal_type_settings_routes


def portable_signal_type_registry(
    configuration: ExternalSourcesConfiguration,
) -> SignalTypeRegistry:
    """Combine built-in dashboard types with configured external types."""
    return SignalTypeRegistry(
        PRODUCTION_SIGNAL_TYPE_REGISTRY.signal_types
        + registry_with_display_overrides(configuration).signal_types
    )


def create_portable_dashboard(
    events_path: Path,
    configuration: ExternalSourcesConfiguration,
    historical_database_path: Path | None = None,
    *,
    detailed_historical_database_paths: Sequence[Path] | None = None,
    hourly_history_database_path: Path | None = None,
    market_data_configuration: MarketDataConfiguration | None = None,
    source_configuration_path: Path | None = None,
) -> FastAPI:
    """Create a dashboard backed only by the supplied EventStore path.

    Historical databases remain optional at startup. Supplied detailed paths are
    ordered oldest-to-newest for the existing read-only chart reader.
    """
    events_path = Path(events_path)
    _validate_events_path(events_path)
    detailed_paths = _detailed_history_paths(
        historical_database_path, detailed_historical_database_paths
    )
    if market_data_configuration is not None:
        if detailed_paths or hourly_history_database_path is not None:
            raise ValueError(
                "Market data configuration cannot be combined with --history-path"
            )
        validate_local_market_data(market_data_configuration)
    if hourly_history_database_path is not None:
        hourly_history_database_path = Path(hourly_history_database_path)
        _validate_history_path(hourly_history_database_path)
        if not detailed_paths:
            raise ValueError(
                "Hourly history requires at least one detailed history path"
            )
    registry = portable_signal_type_registry(configuration)
    for source in configuration.sources:
        if set(source.type_mapping.values()) - set(registry.by_event_type):
            raise ValueError("Source mapping targets an unregistered dashboard type")
    EventStore(
        events_path,
        allowed_event_types=tuple(item.event_type for item in registry.signal_types),
    ).initialize()
    def current_configuration() -> ExternalSourcesConfiguration:
        return (
            configuration
            if source_configuration_path is None
            else load_external_sources_configuration(source_configuration_path)
        )

    def current_registry() -> SignalTypeRegistry:
        return portable_signal_type_registry(current_configuration())

    def source_visibility() -> list[tuple[str, bool]]:
        return [
            (source.source_id, source.is_visible)
            for source in current_configuration().sources
        ]

    def custom_signals() -> tuple[CustomSignalDefinition, ...]:
        return current_configuration().custom_signals

    def card_layouts() -> dict[
        tuple[str, str], tuple[tuple[str, ...], tuple[str, ...]]
    ]:
        return {
            (source.source_id, type_id): (
                layout.compact_fields,
                layout.detail_fields,
            )
            for source in current_configuration().sources
            for type_id, layout in source.card_layouts.items()
        }

    app = create_app(
        events_path,
        historical_database_path=detailed_paths[0] if detailed_paths else None,
        hourly_history_database_path=hourly_history_database_path,
        detailed_historical_database_paths=detailed_paths,
        market_data_source=(
            None
            if market_data_configuration is None
            else market_data_configuration.provider
        ),
        signal_type_registry=registry,
        registry_provider=current_registry,
        visibility_provider=source_visibility,
        custom_signals_provider=custom_signals,
        card_layouts_provider=card_layouts,
    )
    if source_configuration_path is not None:
        add_signal_type_settings_routes(
            app,
            events_path=events_path,
            configuration_path=source_configuration_path,
            signal_type_registry=registry,
        )
        attach_source_management(
            app,
            events_path=events_path,
            configuration_path=source_configuration_path,
            allowed_database_roots=None,
            registry_provider=current_registry,
        )
    return app


def _validate_events_path(events_path: Path) -> None:
    if events_path.exists() and not events_path.is_file():
        raise ValueError(f"EventStore path is not a file: {events_path}")
    if events_path.parent.exists() and not events_path.parent.is_dir():
        raise ValueError(f"EventStore parent is not a directory: {events_path.parent}")


def _validate_history_path(historical_database_path: Path) -> None:
    if not historical_database_path.is_file():
        raise ValueError(
            f"Historical candle database not found: {historical_database_path}"
        )


def _detailed_history_paths(
    historical_database_path: Path | None,
    detailed_historical_database_paths: Sequence[Path] | None,
) -> tuple[Path, ...]:
    paths: tuple[Path, ...]
    if detailed_historical_database_paths is None:
        paths = () if historical_database_path is None else (historical_database_path,)
    else:
        paths = tuple(detailed_historical_database_paths)
    normalized = tuple(Path(path) for path in paths)
    for path in normalized:
        _validate_history_path(path)
    return normalized
