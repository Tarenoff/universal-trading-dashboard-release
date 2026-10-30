"""Read-only FastAPI dashboard for final signal events."""

from __future__ import annotations

import json
import sqlite3
from collections import Counter, defaultdict
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Literal, cast
from zoneinfo import ZoneInfo

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from dashboard.chart_data import (
    DEFAULT_DASHBOARD_HOURLY_HISTORY_PATH,
    DEFAULT_DETAILED_HISTORICAL_DATABASE_PATHS,
    DEFAULT_HISTORICAL_DATABASE_PATH,
    TIMEFRAME_SECONDS,
    ChartData,
    HyperliquidHistoricalCandlesUnavailableError,
    load_binance_chart_data,
    load_bybit_chart_data,
    load_chart_data,
    load_hyperliquid_chart_data,
)
from dashboard.custom_signals import CustomSignalDefinition, project_visible_events
from dashboard.event_store import DEFAULT_DATABASE_PATH, EventStore, SignalEvent
from dashboard.market_data_config import BybitApiMarketData, LocalSqliteMarketData
from dashboard.signal_type_registry import (
    PRODUCTION_SIGNAL_TYPE_REGISTRY,
    SignalTypeRegistry,
)
from dashboard.exchanges.binance import BinanceAPIError, BinanceUsdmPublicClient
from dashboard.exchanges.bybit import BybitAPIError
from dashboard.exchanges.hyperliquid import HyperliquidAPIError

PACKAGE_PATH = Path(__file__).parent
DASHBOARD_JAVASCRIPT_PATH = PACKAGE_PATH / "static" / "dashboard.js"
templates = Jinja2Templates(directory=PACKAGE_PATH / "templates")
MOSCOW_TIMEZONE = ZoneInfo("Europe/Moscow")
FILTER_EVENT_TYPES = {
    item.filter_key: item.event_type
    for item in PRODUCTION_SIGNAL_TYPE_REGISTRY.signal_types
}
EVENT_TYPE_FILTER_KEYS = {
    item.event_type: item.filter_key
    for item in PRODUCTION_SIGNAL_TYPE_REGISTRY.signal_types
}


@dataclass(frozen=True, slots=True)
class SymbolSummary:
    symbol: str
    latest_event: SignalEvent
    event_type_counts: dict[str, int]
    event_type_latest_at: dict[str, str]
    latest_event_time: str


@dataclass(frozen=True, slots=True)
class EventRow:
    event_id: int | None
    event_type: str
    type: str
    display_name: str
    tone: str
    event_at: str
    event_timestamp: str
    event_price: str
    source_interval: str | None
    alert_level: str | None
    direction: str | None
    exchange: str
    primary_signal_id: str
    metadata: dict[str, object]
    compact_fields: tuple[str, ...] | None
    detail_fields: tuple[str, ...] | None


@dataclass(frozen=True, slots=True)
class EventFilter:
    """Normalized canonical signal-type selection for a dashboard view."""

    event_types: tuple[str, ...] = ()
    filter_keys: tuple[str, ...] = ()
    match_mode: Literal["any"] = "any"

    @property
    def url_value(self) -> str:
        """Return the legacy filter keys in registry order for URLs."""
        if not self.event_types:
            return "all"
        return ",".join(
            self.filter_keys
            or tuple(
                EVENT_TYPE_FILTER_KEYS[event_type] for event_type in self.event_types
            )
        )

    def matches_symbol_events(self, events: Sequence[SignalEvent]) -> bool:
        """Return whether a symbol has a selected signal type.

        ``match_mode`` is explicit so a future all-types mode can change this
        group-level predicate without changing callers.
        """
        if not self.event_types:
            return True
        selected_types = set(self.event_types)
        event_types = {event.event_type for event in events}
        if self.match_mode == "any":
            return bool(event_types & selected_types)
        return selected_types <= event_types

    def matches_event(self, event: SignalEvent) -> bool:
        """Return whether an event contributes to the selected symbol view."""
        return not self.event_types or event.event_type in self.event_types


def _parse_event_filter(
    value: str,
    registry: SignalTypeRegistry = PRODUCTION_SIGNAL_TYPE_REGISTRY,
) -> EventFilter:
    """Parse legacy or comma-separated filter keys into canonical event types."""
    if value == "all":
        return EventFilter()
    filter_keys = value.split(",")
    registry_by_filter_key = registry.by_filter_key
    if not filter_keys or any(key not in registry_by_filter_key for key in filter_keys):
        raise ValueError("Unsupported event filter")
    selected_keys = set(filter_keys)
    return EventFilter(
        tuple(
            item.event_type
            for item in registry.signal_types
            if item.filter_key in selected_keys
        ),
        tuple(
            item.filter_key
            for item in registry.signal_types
            if item.filter_key in selected_keys
        ),
    )


def create_app(
    database_path: Path | str = DEFAULT_DATABASE_PATH,
    historical_database_path: Path | str | None = DEFAULT_HISTORICAL_DATABASE_PATH,
    hourly_history_database_path: Path
    | str
    | None = DEFAULT_DASHBOARD_HOURLY_HISTORY_PATH,
    detailed_historical_database_paths: Sequence[Path | str] | None = None,
    market_data_source: LocalSqliteMarketData | BybitApiMarketData | None = None,
    signal_type_registry: SignalTypeRegistry = PRODUCTION_SIGNAL_TYPE_REGISTRY,
    refresh_events: Callable[[], None] | None = None,
    registry_provider: Callable[[], SignalTypeRegistry] | None = None,
    visibility_provider: Callable[[], Sequence[tuple[str, bool]]] | None = None,
    custom_signals_provider: (
        Callable[[], Sequence[CustomSignalDefinition]] | None
    ) = None,
    card_layouts_provider: (
        Callable[[], Mapping[tuple[str, str], tuple[tuple[str, ...], tuple[str, ...]]]]
        | None
    ) = None,
) -> FastAPI:
    """Create a dashboard application backed by one read-only SQLite database."""
    app = FastAPI(title="Signal Dashboard", docs_url=None, redoc_url=None)
    app.mount("/static", StaticFiles(directory=PACKAGE_PATH / "static"), name="static")
    store = EventStore(
        database_path,
        allowed_event_types=tuple(
            item.event_type for item in signal_type_registry.signal_types
        ),
    )
    historical_path = (
        None if historical_database_path is None else Path(historical_database_path)
    )
    detailed_history_paths = (
        tuple(Path(path) for path in detailed_historical_database_paths)
        if detailed_historical_database_paths is not None
        else (
            DEFAULT_DETAILED_HISTORICAL_DATABASE_PATHS
            if historical_path == DEFAULT_HISTORICAL_DATABASE_PATH
            else (() if historical_path is None else (historical_path,))
        )
    )
    hourly_history_path = (
        None
        if hourly_history_database_path is None
        else Path(hourly_history_database_path)
    )
    local_market_data_available = (
        historical_path is not None
        if market_data_source is None
        else isinstance(market_data_source, LocalSqliteMarketData)
    )
    default_market_source = (
        "bybit_api"
        if isinstance(market_data_source, BybitApiMarketData)
        or not local_market_data_available
        else "local_sqlite"
    )

    def current_registry() -> SignalTypeRegistry:
        return registry_provider() if registry_provider else signal_type_registry

    def current_card_layouts(
    ) -> Mapping[tuple[str, str], tuple[tuple[str, ...], tuple[str, ...]]]:
        return card_layouts_provider() if card_layouts_provider else {}

    def visible_events(symbol: str | None = None) -> list[SignalEvent]:
        events = store.list_events(symbol=symbol)
        if custom_signals_provider is None:
            if visibility_provider is None:
                return cast(list[SignalEvent], events)
            return filter_source_visibility(events)
        custom_signals = custom_signals_provider()
        projected = project_visible_events(
            events,
            source_visibility=dict(
                visibility_provider() if visibility_provider else ()
            ),
            custom_signals=custom_signals,
        )
        if visibility_provider is None:
            return cast(list[SignalEvent], projected)
        custom_type_ids = {definition.signal_id for definition in custom_signals}
        return [
            event
            for event in projected
            if event.event_type in custom_type_ids
            or event.owner_source_id is not None
            or filter_source_visibility((event,))
        ]

    def filter_source_visibility(events: Sequence[SignalEvent]) -> list[SignalEvent]:
        provider = visibility_provider
        assert provider is not None
        sources = sorted(
            (
                (f"external:{source_id}:", visible)
                for source_id, visible in provider()
            ),
            key=lambda item: len(item[0]),
            reverse=True,
        )
        return [
            event
            for event in events
            if next(
                (
                    visible
                    for prefix, visible in sources
                    if event.delivery_identity.startswith(prefix)
                ),
                True,
            )
        ]

    @app.get("/", response_class=HTMLResponse)
    def dashboard(
        request: Request,
        symbol: str | None = Query(default=None),
        event_filter: str = Query(default="all", alias="filter"),
    ) -> HTMLResponse:
        signal_type_registry = current_registry()
        if refresh_events is not None:
            refresh_events()
        try:
            selected_filter = _parse_event_filter(event_filter, signal_type_registry)
        except ValueError:
            raise HTTPException(status_code=422, detail="Unsupported event filter")
        events = visible_events()
        available_types = {event.event_type for event in events}
        if not set(selected_filter.event_types) <= available_types:
            selected_filter = EventFilter()
        summaries = _summarize_events(events, event_filter=selected_filter)
        selected = next((item for item in summaries if item.symbol == symbol), None)
        if selected is None and summaries:
            selected = summaries[0]
        return templates.TemplateResponse(
            request,
            "dashboard.html",
            {
                "symbols": summaries,
                "selected": selected,
                "event_filter": selected_filter.url_value,
                "available_event_types": sorted(available_types),
                "dashboard_javascript_version": (
                    DASHBOARD_JAVASCRIPT_PATH.stat().st_mtime_ns
                ),
                "signal_type_registry": signal_type_registry.frontend_definitions(),
                "market_data_sources": (
                    {"id": "bybit_api", "label": "Bybit", "available": True},
                    {
                        "id": "binance_usdm_api",
                        "label": "Binance",
                        "available": True,
                    },
                    {
                        "id": "hyperliquid_api",
                        "label": "Hyperliquid",
                        "available": True,
                    },
                ),
                "default_market_source": default_market_source,
                "selected_event_time": (
                    _format_moscow_datetime(selected.latest_event.event_at)
                    if selected
                    else None
                ),
                "selected_events": (
                    _event_rows(
                        [
                            event
                            for event in events
                            if event.symbol == selected.symbol
                            and selected_filter.matches_event(event)
                        ],
                        signal_type_registry,
                        current_card_layouts(),
                    )
                    if selected
                    else []
                ),
            },
        )

    @app.get("/api/chart")
    def chart_data(
        symbol: str = Query(min_length=1),
        timeframe: str = Query(default="5m"),
        event_filter: str = Query(default="all", alias="filter"),
        before: int | None = Query(default=None, ge=0),
        after: int | None = Query(default=None, ge=0),
        market_source: Literal[
            "local_sqlite", "bybit_api", "binance_usdm_api", "hyperliquid_api"
        ] | None = Query(
            default=None
        ),
    ) -> ChartData:
        signal_type_registry = current_registry()
        if timeframe not in TIMEFRAME_SECONDS:
            raise HTTPException(status_code=422, detail="Unsupported timeframe")
        if before is not None and after is not None:
            raise HTTPException(
                status_code=422, detail="Use either before or after, not both"
            )
        try:
            selected_filter = _parse_event_filter(event_filter, signal_type_registry)
        except ValueError:
            raise HTTPException(status_code=422, detail="Unsupported event filter")
        selected_market_source = (
            default_market_source if market_source is None else market_source
        )
        selected_events = [
            event
            for event in visible_events(symbol=symbol)
            if selected_filter.matches_event(event)
        ]
        marker_labels = {
            item.event_type: item.short_label
            for item in signal_type_registry.signal_types
        }
        if selected_market_source == "bybit_api":
            try:
                result = load_bybit_chart_data(
                    symbol=symbol,
                    timeframe=timeframe,
                    events=selected_events,
                    before=before,
                    after=after,
                    marker_labels=marker_labels,
                )
            except (BybitAPIError, ValueError) as exc:
                raise HTTPException(
                    status_code=503,
                    detail=f"Market data is unavailable for symbol: {symbol}",
                ) from exc
            if not result["candles"] and before is None:
                raise HTTPException(
                    status_code=503,
                    detail=f"Market data is unavailable for symbol: {symbol}",
                )
            return result

        if selected_market_source == "binance_usdm_api":
            try:
                result = load_binance_chart_data(
                    symbol=symbol,
                    timeframe=timeframe,
                    events=selected_events,
                    before=before,
                    after=after,
                    marker_labels=marker_labels,
                )
            except (BinanceAPIError, ValueError) as exc:
                raise HTTPException(
                    status_code=503,
                    detail=f"Market data is unavailable for symbol: {symbol}",
                ) from exc
            if not result["candles"] and before is None:
                raise HTTPException(
                    status_code=503,
                    detail=f"Market data is unavailable for symbol: {symbol}",
                )
            return result

        if selected_market_source == "hyperliquid_api":
            try:
                result = load_hyperliquid_chart_data(
                    symbol=symbol,
                    timeframe=timeframe,
                    events=selected_events,
                    before=before,
                    after=after,
                    marker_labels=marker_labels,
                )
            except HyperliquidHistoricalCandlesUnavailableError as exc:
                raise HTTPException(status_code=410, detail=str(exc)) from exc
            except ValueError as exc:
                detail = (
                    f"Hyperliquid does not support timeframe: {timeframe}"
                    if str(exc).startswith("unsupported Hyperliquid chart timeframe")
                    else f"Market data is unavailable for symbol: {symbol}"
                )
                raise HTTPException(status_code=503, detail=detail) from exc
            except HyperliquidAPIError as exc:
                raise HTTPException(
                    status_code=503,
                    detail=f"Market data is unavailable for symbol: {symbol}",
                ) from exc
            if not result["candles"] and before is None:
                raise HTTPException(
                    status_code=503,
                    detail=f"Market data is unavailable for symbol: {symbol}",
                )
            return result
        if not local_market_data_available:
            raise HTTPException(
                status_code=503,
                detail="Local SQLite market data is not configured",
            )
        selected_history_path = (
            historical_path
            if market_data_source is None
            else market_data_source.detailed_history_paths[0]
        )
        if selected_history_path is None:
            raise HTTPException(
                status_code=503,
                detail="Historical candle database is not configured",
            )
        try:
            result = load_chart_data(
                selected_history_path,
                detailed_database_paths=(
                    detailed_history_paths
                    if market_data_source is None
                    else market_data_source.detailed_history_paths
                ),
                hourly_database_path=(
                    hourly_history_path
                    if market_data_source is None
                    else market_data_source.hourly_history_path
                ),
                symbol=symbol,
                timeframe=timeframe,
                events=selected_events,
                before=before,
                after=after,
                marker_labels=marker_labels,
            )
            if market_data_source is not None and not result["candles"]:
                raise HTTPException(
                    status_code=503,
                    detail=f"Market data is unavailable for symbol: {symbol}",
                )
            return result
        except (FileNotFoundError, sqlite3.Error) as exc:
            raise HTTPException(
                status_code=503, detail="Historical candle database is unavailable"
            ) from exc

    @app.get("/api/market-summary/binance")
    def binance_market_summary(symbol: str = Query(min_length=1)) -> dict[str, str]:
        try:
            with BinanceUsdmPublicClient() as client:
                ticker = client.get_24h_ticker(symbol)
                mark_price = client.get_mark_price(symbol)
        except (BinanceAPIError, ValueError) as exc:
            raise HTTPException(
                status_code=503,
                detail=f"Binance market summary is unavailable for symbol: {symbol}",
            ) from exc
        return {
            "symbol": ticker.symbol,
            "last_price": str(ticker.last_price),
            "price_change": str(ticker.price_change),
            "price_change_percent": str(ticker.price_change_percent),
            "high_price": str(ticker.high_price),
            "low_price": str(ticker.low_price),
            "quote_volume": str(ticker.quote_volume),
            "last_funding_rate": str(mark_price.last_funding_rate),
            "next_funding_time": str(
                int(mark_price.next_funding_time.timestamp() * 1_000)
            ),
        }

    @app.get("/api/dashboard")
    def dashboard_data(
        symbol: str | None = Query(default=None, min_length=1),
        event_filter: str = Query(default="all", alias="filter"),
        local_event_filter: str | None = Query(default=None, alias="local_filter"),
    ) -> dict[str, object]:
        """Return data needed to refresh an already-selected dashboard view."""
        signal_type_registry = current_registry()
        if refresh_events is not None:
            refresh_events()
        try:
            selected_filter = _parse_event_filter(event_filter, signal_type_registry)
            selected_local_filter = _parse_event_filter(
                local_event_filter
                if isinstance(local_event_filter, str)
                else event_filter,
                signal_type_registry,
            )
        except ValueError:
            raise HTTPException(status_code=422, detail="Unsupported event filter")
        events = visible_events()
        available_types = {event.event_type for event in events}
        if not set(selected_filter.event_types) <= available_types:
            selected_filter = EventFilter()
            if (
                not isinstance(local_event_filter, str)
                or local_event_filter == event_filter
            ):
                selected_local_filter = EventFilter()
        return {
            "available_event_types": sorted(available_types),
            "active_filter": selected_filter.url_value,
            "symbols": [
                asdict(item)
                for item in _summarize_events(events, event_filter=selected_filter)
            ],
            "events": [
                asdict(item)
                for item in _event_rows(
                    [
                        event
                        for event in events
                        if event.symbol == symbol
                        and selected_local_filter.matches_event(event)
                    ]
                    if symbol
                    else [],
                    signal_type_registry,
                    current_card_layouts(),
                )
            ],
        }

    return app


def _summarize_events(
    events: list[SignalEvent],
    *,
    event_filter: EventFilter | str = "all",
    now: datetime | None = None,
) -> list[SymbolSummary]:
    """Return symbol summaries ordered by the active signal type."""
    selected_filter = (
        _parse_event_filter(event_filter)
        if isinstance(event_filter, str)
        else event_filter
    )
    grouped: defaultdict[str, list[SignalEvent]] = defaultdict(list)
    for event in events:
        grouped[event.symbol].append(event)

    summaries: list[SymbolSummary] = []
    for symbol, items in grouped.items():
        if not selected_filter.matches_symbol_events(items):
            continue
        relevant_events = [
            event for event in items if selected_filter.matches_event(event)
        ]
        latest_event = max(
            relevant_events, key=lambda event: (event.event_at, event.created_at)
        )
        summaries.append(
            SymbolSummary(
                symbol=symbol,
                latest_event=latest_event,
                event_type_counts=dict(
                    Counter(event.event_type for event in relevant_events)
                ),
                event_type_latest_at={
                    event_type: max(
                        event.event_at
                        for event in relevant_events
                        if event.event_type == event_type
                    )
                    for event_type in {event.event_type for event in relevant_events}
                },
                latest_event_time=_format_sidebar_time(latest_event.event_at, now=now),
            )
        )
    return sorted(
        summaries,
        key=lambda item: (
            item.latest_event.event_at,
            item.latest_event.created_at,
            item.symbol,
        ),
        reverse=True,
    )


def _parse_utc_timestamp(timestamp: str) -> datetime:
    return datetime.fromisoformat(timestamp.replace("Z", "+00:00"))


def _format_moscow_datetime(timestamp: str) -> str:
    """Format a stored UTC timestamp for a Moscow-time dashboard detail."""
    return (
        _parse_utc_timestamp(timestamp)
        .astimezone(MOSCOW_TIMEZONE)
        .strftime("%d.%m.%Y %H:%M MSK")
    )


def _format_sidebar_time(timestamp: str, *, now: datetime | None = None) -> str:
    """Format a stored UTC timestamp using the Moscow calendar day."""
    event_time = _parse_utc_timestamp(timestamp).astimezone(MOSCOW_TIMEZONE)
    current_time = now or datetime.now(MOSCOW_TIMEZONE)
    current_time = current_time.astimezone(MOSCOW_TIMEZONE)
    if event_time.date() != current_time.date():
        return event_time.strftime("%d.%m %H:%M")
    return f"Today {event_time:%H:%M}"


def _event_rows(
    events: list[SignalEvent],
    registry: SignalTypeRegistry = PRODUCTION_SIGNAL_TYPE_REGISTRY,
    card_layouts: Mapping[tuple[str, str], tuple[tuple[str, ...], tuple[str, ...]]]
    | None = None,
) -> list[EventRow]:
    """Return all selected-symbol events in newest-first dashboard order."""
    return [
        EventRow(
            event_id=event.event_id,
            event_type=event.event_type,
            type=registry.by_event_type.get(event.event_type, None).short_label
            if event.event_type in registry.by_event_type
            else event.event_type,
            display_name=registry.by_event_type.get(event.event_type, None).display_name
            if event.event_type in registry.by_event_type
            else event.event_type,
            tone=registry.by_event_type.get(event.event_type, None).card_tone
            if event.event_type in registry.by_event_type
            else "default",
            event_at=_format_moscow_datetime(event.event_at),
            event_timestamp=event.event_at,
            event_price=event.event_price,
            source_interval=event.source_interval,
            alert_level=event.alert_level,
            direction=event.direction,
            exchange=event.exchange,
            primary_signal_id=event.primary_signal_id,
            metadata=json.loads(event.metadata_json),
            compact_fields=layout[0] if layout else None,
            detail_fields=layout[1] if layout else None,
        )
        for event in sorted(
            events, key=lambda event: (event.event_at, event.created_at), reverse=True
        )
        for layout in (
            None
            if event.owner_source_id is None
            else (card_layouts or {}).get((event.owner_source_id, event.event_type)),
        )
    ]


app = create_app()
