"""Read-only candles and event markers for the dashboard chart."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator, Sequence
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from time import monotonic
from typing import Final, cast

from dashboard.event_store import SignalEvent
from dashboard.signal_type_registry import PRODUCTION_SIGNAL_TYPE_REGISTRY
from dashboard.exchanges.binance import (
    HISTORICAL_OPEN_INTEREST_AVAILABILITY,
    BinanceAPIError,
    BinanceUsdmPublicClient,
)
from dashboard.exchanges.bybit import (
    MAX_KLINE_LIMIT,
    BybitAPIError,
    BybitPublicClient,
)
from dashboard.exchanges.hyperliquid import (
    MAX_CANDLES_PER_RESPONSE as HYPERLIQUID_MAX_CANDLES_PER_RESPONSE,
)
from dashboard.exchanges.hyperliquid import (
    HyperliquidPublicClient,
)
from dashboard.exchanges.models import Candle, OpenInterestPoint

DEFAULT_HISTORICAL_DATABASE_PATH: Final = Path(
    "data/historical/bybit_market_data_from_2026-07-15.sqlite3"
)
DEFAULT_DETAILED_HISTORICAL_DATABASE_PATHS: Final = (
    Path("data/historical/bybit_market_data_2026-06-01_2026-06-30.sqlite3"),
    Path("data/historical/bybit_market_data_2026-07-01_2026-07-17.sqlite3"),
    DEFAULT_HISTORICAL_DATABASE_PATH,
)
DEFAULT_DASHBOARD_HOURLY_HISTORY_PATH: Final = Path(
    "data/historical/dashboard_hourly_1y.sqlite3"
)
HOURLY_HISTORY_BOUNDARY: Final = datetime(2026, 6, 1, tzinfo=UTC)
HOURLY_HISTORY_BOUNDARY_TIMESTAMP: Final = int(HOURLY_HISTORY_BOUNDARY.timestamp())
HOURLY_HISTORY_TIMEFRAMES: Final = frozenset(
    {"1h", "2h", "4h", "6h", "12h", "1D", "1W", "1M"}
)
INTRADAY_TIMEFRAMES: Final = frozenset({"1m", "3m", "5m", "15m", "30m"})
TIMEFRAME_SECONDS: Final = {
    "1m": 60,
    "3m": 3 * 60,
    "5m": 5 * 60,
    "15m": 15 * 60,
    "30m": 30 * 60,
    "1h": 60 * 60,
    "2h": 2 * 60 * 60,
    "4h": 4 * 60 * 60,
    "6h": 6 * 60 * 60,
    "12h": 12 * 60 * 60,
    "1D": 24 * 60 * 60,
    "1W": 7 * 24 * 60 * 60,
    "1M": 30 * 24 * 60 * 60,
}
OPEN_INTEREST_INTERVALS: Final = {
    "1m": "5min",
    "3m": "5min",
    "5m": "5min",
    "15m": "15min",
    "30m": "15min",
    "1h": "1h",
    "2h": "1h",
    "4h": "1h",
    "6h": "1h",
    "12h": "1h",
    "1D": "1h",
    "1W": "1h",
    "1M": "1h",
}
CHART_CANDLE_LIMIT: Final = 1_500
ONE_MINUTE: Final = timedelta(minutes=1)
REST_TAIL_CACHE_SECONDS: Final = 30.0
BYBIT_API_INTERVALS: Final = {
    "1m": "1",
    "3m": "3",
    "5m": "5",
    "15m": "15",
    "30m": "30",
    "1h": "60",
    "2h": "120",
    "4h": "240",
    "6h": "360",
    "12h": "720",
    "1D": "D",
    "1W": "W",
}
MAX_BYBIT_CANDLE_PAGES: Final = 2
MAX_BYBIT_OI_PAGES: Final = 10
BINANCE_API_INTERVALS: Final = {
    "1m": "1m",
    "3m": "3m",
    "5m": "5m",
    "15m": "15m",
    "30m": "30m",
    "1h": "1h",
    "2h": "2h",
    "4h": "4h",
    "6h": "6h",
    "12h": "12h",
    "1D": "1d",
    "1W": "1w",
    "1M": "1M",
}
BINANCE_OPEN_INTEREST_INTERVALS: Final = {
    "5m": "5m",
    "15m": "15m",
    "30m": "30m",
    "1h": "1h",
    "2h": "2h",
    "4h": "4h",
    "6h": "6h",
    "12h": "12h",
    "1D": "1d",
}
HYPERLIQUID_API_INTERVALS: Final = {
    "1m": "1m",
    "3m": "3m",
    "5m": "5m",
    "15m": "15m",
    "30m": "30m",
    "1h": "1h",
    "2h": "2h",
    "4h": "4h",
    "12h": "12h",
    "1D": "1d",
    "1W": "1w",
    "1M": "1M",
}
ChartData = dict[str, object]


class HyperliquidHistoricalCandlesUnavailableError(ValueError):
    """Raised only for a range definitively older than API candle retention."""

    def __init__(self, suggested_timeframes: tuple[str, ...]) -> None:
        self.suggested_timeframes = suggested_timeframes
        message = "Hyperliquid historical candles are unavailable for this period."
        if suggested_timeframes:
            suggestions = " or ".join(suggested_timeframes)
            message += f" Try {suggestions} to check whether it covers this date."
        super().__init__(message)


@dataclass(frozen=True, slots=True)
class _CachedTail:
    fetched_at: float
    candles: list[Candle]
    open_interest: list[OpenInterestPoint]


_rest_tail_cache: dict[tuple[str, str], _CachedTail] = {}


def load_chart_data(
    database_path: Path | str,
    *,
    detailed_database_paths: Sequence[Path | str] | None = None,
    hourly_database_path: Path | None = None,
    symbol: str,
    timeframe: str,
    events: list[SignalEvent],
    before: int | None = None,
    after: int | None = None,
    now: datetime | None = None,
    marker_labels: dict[str, str] | None = None,
) -> ChartData:
    """Return recent UTC candles and markers for one Bybit symbol.

    The historical cache stores one-minute candles. SQL aggregates those into
    chart timeframes, including UTC calendar-day buckets for the 1D chart.
    """
    seconds = TIMEFRAME_SECONDS.get(timeframe)
    if seconds is None:
        raise ValueError(f"unsupported timeframe: {timeframe}")
    if before is not None and after is not None:
        raise ValueError("before and after cannot be used together")
    history_boundary = (
        _detailed_history_boundary_metadata()
        if timeframe in INTRADAY_TIMEFRAMES
        else None
    )
    detailed_paths = tuple(
        Path(path)
        for path in (detailed_database_paths or (database_path,))
    )
    hourly_context = _optional_readonly_connection(
        hourly_database_path if timeframe in HOURLY_HISTORY_TIMEFRAMES else None
    )
    with (
        _readonly_connections(detailed_paths) as detailed_connections,
        hourly_context as hourly_connection,
    ):
        if timeframe in HOURLY_HISTORY_TIMEFRAMES and hourly_connection is not None:
            candles, has_older, has_newer = _load_candles_with_hourly_history(
                detailed_connections,
                hourly_connection,
                symbol=symbol,
                seconds=seconds,
                before=before,
                after=after,
            )
        else:
            candles, has_older, has_newer = _load_detailed_candles(
                detailed_connections,
                symbol=symbol,
                seconds=seconds,
                before=before,
                after=after,
            )
        open_interest = _load_detailed_open_interest(
            detailed_connections,
            symbol=symbol,
            seconds=seconds,
            source_interval=OPEN_INTEREST_INTERVALS[timeframe],
            start=int(candles[0]["time"]) if candles else None,
            end=(int(candles[-1]["time"]) + seconds - 1) if candles else None,
        )
    freshness = {"status": "local"}
    if before is None and after is None:
        candles, open_interest, has_older, freshness = _append_rest_tail(
            candles,
            open_interest,
            has_older=has_older,
            symbol=symbol,
            timeframe=timeframe,
            seconds=seconds,
            now=now or datetime.now(UTC),
        )
    candle_times = {int(candle["time"]) for candle in candles}
    markers = _markers_for_candles(
        events,
        seconds=seconds,
        candle_times=candle_times,
        marker_labels=marker_labels,
    )
    return {
        "candles": candles,
        "volume": [
            {"time": int(candle["time"]), "value": float(candle["volume"])}
            for candle in candles
        ],
        "openInterest": open_interest,
        "markers": markers,
        "hasOlder": has_older,
        "hasNewer": has_newer,
        "freshness": freshness,
        "historyBoundary": history_boundary,
    }


def load_bybit_chart_data(
    *,
    symbol: str,
    timeframe: str,
    events: list[SignalEvent],
    before: int | None = None,
    after: int | None = None,
    now: datetime | None = None,
    marker_labels: dict[str, str] | None = None,
) -> ChartData:
    """Return one bounded on-demand Bybit chart page without local fallback."""
    interval = BYBIT_API_INTERVALS.get(timeframe)
    seconds = TIMEFRAME_SECONDS.get(timeframe)
    if interval is None or seconds is None:
        raise ValueError(f"unsupported Bybit chart timeframe: {timeframe}")
    if before is not None and after is not None:
        raise ValueError("before and after cannot be used together")
    end = _bybit_page_end(before=before, after=after, seconds=seconds, now=now)
    start = end - timedelta(seconds=seconds * (CHART_CANDLE_LIMIT - 1))
    candles, has_older = _bybit_candles(
        symbol=symbol, interval=interval, seconds=seconds, start=start, end=end
    )
    ois, oi_status = _bybit_open_interest(
        symbol=symbol,
        timeframe=timeframe,
        start=start,
        end=end,
    )
    chart_candles = [
        {
            "time": int(candle.open_time.astimezone(UTC).timestamp()),
            "open": float(candle.open),
            "high": float(candle.high),
            "low": float(candle.low),
            "close": float(candle.close),
            "volume": float(candle.volume),
        }
        for candle in candles
    ]
    candle_times = {int(candle["time"]) for candle in chart_candles}
    return {
        "candles": chart_candles,
        "volume": [
            {"time": int(candle["time"]), "value": float(candle["volume"])}
            for candle in chart_candles
        ],
        "openInterest": _aggregate_open_interest(ois, seconds=seconds),
        "openInterestStatus": oi_status,
        "markers": _markers_for_candles(
            events,
            seconds=seconds,
            candle_times=candle_times,
            marker_labels=marker_labels,
        ),
        "hasOlder": has_older,
        "hasNewer": after is not None and len(chart_candles) == CHART_CANDLE_LIMIT,
        "freshness": {"status": "remote"},
        "historyBoundary": None,
    }


def load_binance_chart_data(
    *,
    symbol: str,
    timeframe: str,
    events: list[SignalEvent],
    before: int | None = None,
    after: int | None = None,
    now: datetime | None = None,
    marker_labels: dict[str, str] | None = None,
) -> ChartData:
    """Return one bounded Binance USDⓈ-M chart page without source fallback."""
    interval = BINANCE_API_INTERVALS.get(timeframe)
    seconds = TIMEFRAME_SECONDS.get(timeframe)
    if interval is None or seconds is None:
        raise ValueError(f"unsupported Binance chart timeframe: {timeframe}")
    if before is not None and after is not None:
        raise ValueError("before and after cannot be used together")
    observed = datetime.now(UTC) if now is None else now.astimezone(UTC)
    end = _binance_page_end(
        before=before,
        after=after,
        timeframe=timeframe,
        seconds=seconds,
        now=observed,
    )
    start = _binance_page_start(end=end, timeframe=timeframe, seconds=seconds)
    with BinanceUsdmPublicClient() as client:
        client.get_usdm_perpetual_symbol(symbol)
        candles, has_older = _binance_candles(
            client,
            symbol=symbol,
            interval=interval,
            timeframe=timeframe,
            seconds=seconds,
            start=None if before is None and after is None else start,
            end=end,
        )
        ois, oi_status = _binance_open_interest(
            client,
            symbol=symbol,
            timeframe=timeframe,
            start=start,
            end=end,
            now=observed,
        )
    chart_candles = [
        {
            "time": int(candle.open_time.astimezone(UTC).timestamp()),
            "open": float(candle.open),
            "high": float(candle.high),
            "low": float(candle.low),
            "close": float(candle.close),
            "volume": float(candle.volume),
        }
        for candle in candles
    ]
    candle_times = {int(candle["time"]) for candle in chart_candles}
    return {
        "candles": chart_candles,
        "volume": [
            {"time": int(candle["time"]), "value": float(candle["volume"])}
            for candle in chart_candles
        ],
        "openInterest": [
            {
                "time": int(point.timestamp.astimezone(UTC).timestamp()),
                "value": float(point.open_interest),
            }
            for point in ois
        ],
        "openInterestStatus": oi_status,
        "markers": _markers_for_candles(
            events,
            seconds=seconds,
            candle_times=candle_times,
            marker_labels=marker_labels,
        ),
        "hasOlder": has_older,
        "hasNewer": after is not None and len(chart_candles) == CHART_CANDLE_LIMIT,
        "freshness": {"status": "remote"},
        "historyBoundary": None,
    }


def load_hyperliquid_chart_data(
    *,
    symbol: str,
    timeframe: str,
    events: list[SignalEvent],
    before: int | None = None,
    after: int | None = None,
    now: datetime | None = None,
    marker_labels: dict[str, str] | None = None,
) -> ChartData:
    """Return one bounded Hyperliquid standard-perpetual chart page."""
    interval = HYPERLIQUID_API_INTERVALS.get(timeframe)
    seconds = TIMEFRAME_SECONDS.get(timeframe)
    if interval is None or seconds is None:
        raise ValueError(f"unsupported Hyperliquid chart timeframe: {timeframe}")
    if before is not None and after is not None:
        raise ValueError("before and after cannot be used together")
    observed = datetime.now(UTC) if now is None else now.astimezone(UTC)
    end = _binance_page_end(
        before=before,
        after=after,
        timeframe=timeframe,
        seconds=seconds,
        now=observed,
    )
    start = _binance_page_start(end=end, timeframe=timeframe, seconds=seconds)
    max_pages = _hyperliquid_candle_pages()
    with HyperliquidPublicClient() as client:
        requested = client.get_historical_candles(
            symbol,
            interval,
            start=start,
            end=end,
            max_pages=max_pages,
        )
        if (
            not requested
            and before is not None
            and _hyperliquid_is_beyond_retention(
                end=end, timeframe=timeframe, seconds=seconds, now=observed
            )
        ):
            raise HyperliquidHistoricalCandlesUnavailableError(
                _hyperliquid_timeframe_suggestions(
                    requested_end=end, timeframe=timeframe, now=observed
                )
            )
        candles_by_time = {
            candle.open_time.astimezone(UTC): candle
            for candle in requested
            if start <= candle.open_time.astimezone(UTC) <= end
        }
        candles = [
            candles_by_time[timestamp]
            for timestamp in sorted(candles_by_time)[-CHART_CANDLE_LIMIT:]
        ]
        has_older = _hyperliquid_has_older_candles(
            client,
            symbol=symbol,
            interval=interval,
            timeframe=timeframe,
            seconds=seconds,
            candles=candles,
        )
    chart_candles = [
        {
            "time": int(candle.open_time.astimezone(UTC).timestamp()),
            "open": float(candle.open),
            "high": float(candle.high),
            "low": float(candle.low),
            "close": float(candle.close),
            "volume": float(candle.volume),
        }
        for candle in candles
    ]
    candle_times = {int(candle["time"]) for candle in chart_candles}
    return {
        "candles": chart_candles,
        "volume": [
            {"time": int(candle["time"]), "value": float(candle["volume"])}
            for candle in chart_candles
        ],
        "openInterest": [],
        "openInterestStatus": {"status": "unavailable"},
        "markers": _markers_for_candles(
            events,
            seconds=seconds,
            candle_times=candle_times,
            marker_labels=marker_labels,
        ),
        "hasOlder": has_older,
        "hasNewer": after is not None and len(chart_candles) == CHART_CANDLE_LIMIT,
        "freshness": {"status": "remote"},
        "historyBoundary": None,
    }


def _hyperliquid_candle_pages() -> int:
    return (
        CHART_CANDLE_LIMIT + HYPERLIQUID_MAX_CANDLES_PER_RESPONSE - 1
    ) // HYPERLIQUID_MAX_CANDLES_PER_RESPONSE


def _hyperliquid_is_beyond_retention(
    *, end: datetime, timeframe: str, seconds: int, now: datetime
) -> bool:
    retention_start = _binance_previous_open(
        _binance_candle_open(
            int(now.timestamp()), timeframe=timeframe, seconds=seconds
        ),
        timeframe=timeframe,
        seconds=seconds,
        count=5_000,
    )
    return end < retention_start


def _hyperliquid_timeframe_suggestions(
    *, requested_end: datetime, timeframe: str, now: datetime
) -> tuple[str, ...]:
    suggestions: list[str] = []
    for candidate in ("5m", "15m", "30m", "1h", "2h", "4h", "12h", "1D", "1W", "1M"):
        candidate_seconds = TIMEFRAME_SECONDS[candidate]
        if candidate_seconds <= TIMEFRAME_SECONDS[timeframe]:
            continue
        if not _hyperliquid_is_beyond_retention(
            end=requested_end,
            timeframe=candidate,
            seconds=candidate_seconds,
            now=now,
        ):
            suggestions.append(candidate)
        if len(suggestions) == 2:
            break
    return tuple(suggestions)


def _hyperliquid_has_older_candles(
    client: HyperliquidPublicClient,
    *,
    symbol: str,
    interval: str,
    timeframe: str,
    seconds: int,
    candles: list[Candle],
) -> bool:
    if not candles:
        return False
    oldest = candles[0].open_time.astimezone(UTC)
    previous = _binance_previous_open(
        oldest, timeframe=timeframe, seconds=seconds
    )
    return any(
        candle.open_time.astimezone(UTC) < oldest
        for candle in client.get_candles(
            symbol, interval, start=previous, end=oldest
        )
    )


def _binance_page_end(
    *,
    before: int | None,
    after: int | None,
    timeframe: str,
    seconds: int,
    now: datetime,
) -> datetime:
    if before is not None:
        return _binance_previous_open(
            _binance_candle_open(before, timeframe=timeframe, seconds=seconds),
            timeframe=timeframe,
            seconds=seconds,
        )
    if after is not None:
        return _binance_next_open(
            _binance_candle_open(after, timeframe=timeframe, seconds=seconds),
            timeframe=timeframe,
            seconds=seconds,
            count=CHART_CANDLE_LIMIT,
        )
    return _binance_candle_open(
        int(now.timestamp()), timeframe=timeframe, seconds=seconds
    )


def _binance_page_start(
    *, end: datetime, timeframe: str, seconds: int
) -> datetime:
    return _binance_previous_open(
        end,
        timeframe=timeframe,
        seconds=seconds,
        count=CHART_CANDLE_LIMIT - 1,
    )


def _binance_candle_open(
    timestamp: int, *, timeframe: str, seconds: int
) -> datetime:
    observed = datetime.fromtimestamp(timestamp, UTC)
    midnight = observed.replace(hour=0, minute=0, second=0, microsecond=0)
    if timeframe == "1W":
        return midnight - timedelta(days=midnight.weekday())
    if timeframe == "1M":
        return midnight.replace(day=1)
    return datetime.fromtimestamp(_floor_to_bucket(timestamp, seconds), UTC)


def _binance_previous_open(
    opened_at: datetime,
    *,
    timeframe: str,
    seconds: int,
    count: int = 1,
) -> datetime:
    if timeframe == "1M":
        year = opened_at.year
        month = opened_at.month - count
        while month < 1:
            year -= 1
            month += 12
        return opened_at.replace(year=year, month=month)
    if timeframe == "1W":
        return opened_at - timedelta(weeks=count)
    return opened_at - timedelta(seconds=seconds * count)


def _binance_next_open(
    opened_at: datetime,
    *,
    timeframe: str,
    seconds: int,
    count: int = 1,
) -> datetime:
    if timeframe == "1M":
        year = opened_at.year
        month = opened_at.month + count
        while month > 12:
            year += 1
            month -= 12
        return opened_at.replace(year=year, month=month)
    if timeframe == "1W":
        return opened_at + timedelta(weeks=count)
    return opened_at + timedelta(seconds=seconds * count)


def _binance_candles(
    client: BinanceUsdmPublicClient,
    *,
    symbol: str,
    interval: str,
    timeframe: str,
    seconds: int,
    start: datetime | None,
    end: datetime,
) -> tuple[list[Candle], bool]:
    request = {"end": end} if start is None else {"start": start, "end": end}
    candles = [
        candle
        for candle in client.get_klines(
            symbol, interval, limit=CHART_CANDLE_LIMIT, **request
        )
        if (start is None or start <= candle.open_time.astimezone(UTC))
        and candle.open_time.astimezone(UTC) <= end
    ]
    if not candles:
        return [], False
    oldest = candles[0].open_time.astimezone(UTC)
    older = client.get_klines(
        symbol,
        interval,
        limit=1,
        end=_binance_previous_open(
            oldest, timeframe=timeframe, seconds=seconds
        ),
    )
    has_older = any(candle.open_time.astimezone(UTC) < oldest for candle in older)
    return candles, has_older


def _binance_open_interest(
    client: BinanceUsdmPublicClient,
    *,
    symbol: str,
    timeframe: str,
    start: datetime,
    end: datetime,
    now: datetime,
) -> tuple[list[OpenInterestPoint], dict[str, str]]:
    interval = BINANCE_OPEN_INTEREST_INTERVALS.get(timeframe)
    if interval is None:
        return [], {"status": "unavailable"}
    if start < now - HISTORICAL_OPEN_INTEREST_AVAILABILITY:
        return [], {"status": "unavailable"}
    try:
        points = client.get_historical_open_interest(
            symbol, interval, start=start, end=end, now=now
        )
    except (BinanceAPIError, ValueError):
        return [], {"status": "unavailable"}
    return points, {"status": "available"}


def _bybit_page_end(
    *, before: int | None, after: int | None, seconds: int, now: datetime | None
) -> datetime:
    if before is not None:
        return _bybit_candle_open(before, seconds=seconds) - timedelta(seconds=seconds)
    if after is not None:
        return _bybit_candle_open(after, seconds=seconds) + timedelta(
            seconds=seconds * CHART_CANDLE_LIMIT
        )
    observed = datetime.now(UTC) if now is None else now.astimezone(UTC)
    return _bybit_candle_open(int(observed.timestamp()), seconds=seconds)


def _bybit_candle_open(timestamp: int, *, seconds: int) -> datetime:
    """Return the UTC opening boundary for an on-demand Bybit candle."""
    observed = datetime.fromtimestamp(timestamp, UTC)
    if seconds == TIMEFRAME_SECONDS["1W"]:
        midnight = observed.replace(hour=0, minute=0, second=0, microsecond=0)
        return midnight - timedelta(days=midnight.weekday())
    return datetime.fromtimestamp(_floor_to_bucket(timestamp, seconds), UTC)


def _bybit_candles(
    *, symbol: str, interval: str, seconds: int, start: datetime, end: datetime
) -> tuple[list[Candle], bool]:
    candles: dict[datetime, Candle] = {}
    span = end - start
    page_span = timedelta(seconds=(MAX_KLINE_LIMIT - 1) * seconds)
    with BybitPublicClient() as client:
        cursor = start
        for _ in range(MAX_BYBIT_CANDLE_PAGES):
            if cursor > end:
                break
            page_end = min(end, cursor + page_span)
            limit = min(
                MAX_KLINE_LIMIT,
                int((page_end - cursor) / timedelta(seconds=seconds)) + 1,
            )
            for candle in client.get_klines(
                symbol, interval, limit=limit, start=cursor, end=page_end
            ):
                opened = candle.open_time.astimezone(UTC)
                if start <= opened <= end:
                    candles[opened] = candle
            cursor = page_end + timedelta(seconds=seconds)
        if span > page_span * MAX_BYBIT_CANDLE_PAGES:
            raise ValueError("Bybit candle page exceeds request limit")
        ordered = [candles[timestamp] for timestamp in sorted(candles)]

        # Ask Bybit for precisely the latest candle before this window,
        # without adding that probe result to the displayed page. This avoids
        # treating either a sparse page or a full listing-boundary page as
        # proof that another page exists.
        older = client.get_klines(
            symbol,
            interval,
            limit=1,
            end=start - timedelta(seconds=seconds),
        )
    has_older = any(candle.open_time.astimezone(UTC) < start for candle in older)
    return ordered, has_older


def _bybit_open_interest(
    *, symbol: str, timeframe: str, start: datetime, end: datetime
) -> tuple[list[OpenInterestPoint], dict[str, str]]:
    pages = 0

    def count_page() -> None:
        nonlocal pages
        pages += 1
        if pages > MAX_BYBIT_OI_PAGES:
            raise BybitAPIError("Bybit open-interest page limit exceeded")

    try:
        with BybitPublicClient() as client:
            points = client.get_historical_open_interest(
                symbol,
                OPEN_INTEREST_INTERVALS[timeframe],
                start=start,
                end=end,
                on_page=count_page,
            )
    except BybitAPIError:
        return [], {"status": "unavailable"}
    return points, {"status": "available"}


def _append_rest_tail(
    local_candles: list[dict[str, float | int]],
    local_open_interest: list[dict[str, float | int]],
    *,
    has_older: bool,
    symbol: str,
    timeframe: str,
    seconds: int,
    now: datetime,
) -> tuple[
    list[dict[str, float | int]],
    list[dict[str, float | int]],
    bool,
    dict[str, str],
]:
    """Merge a read-only Bybit 1m tail into the latest local chart candles."""
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("now must be timezone-aware")
    if not local_candles:
        return local_candles, local_open_interest, has_older, {"status": "local"}

    newest_local = int(local_candles[-1]["time"])
    if newest_local >= int(now.timestamp()):
        return local_candles, local_open_interest, has_older, {"status": "local"}

    try:
        tail = _recent_rest_tail(
            symbol=symbol,
            timeframe=timeframe,
            start=datetime.fromtimestamp(newest_local, UTC),
            end=now.astimezone(UTC).replace(second=0, microsecond=0),
        )
    except BybitAPIError:
        return local_candles, local_open_interest, has_older, {"status": "stale"}

    rest_candles = _aggregate_rest_candles(tail.candles, seconds=seconds)
    rest_open_interest = _aggregate_open_interest(tail.open_interest, seconds=seconds)
    merged = {int(candle["time"]): candle for candle in local_candles}
    merged.update({int(candle["time"]): candle for candle in rest_candles})
    candles = [merged[timestamp] for timestamp in sorted(merged)]
    merged_open_interest = {int(point["time"]): point for point in local_open_interest}
    merged_open_interest.update(
        {int(point["time"]): point for point in rest_open_interest}
    )
    merged_has_older = has_older or len(candles) > CHART_CANDLE_LIMIT
    candles = candles[-CHART_CANDLE_LIMIT:]
    candle_times = {int(candle["time"]) for candle in candles}
    open_interest = [
        merged_open_interest[timestamp]
        for timestamp in sorted(merged_open_interest)
        if timestamp in candle_times
    ]
    return candles, open_interest, merged_has_older, {"status": "fresh"}


def _recent_rest_tail(
    *, symbol: str, timeframe: str, start: datetime, end: datetime
) -> _CachedTail:
    """Return a short-lived cached Bybit tail, without persisting it."""
    key = (symbol, timeframe)
    cached = _rest_tail_cache.get(key)
    if cached is not None and monotonic() - cached.fetched_at < REST_TAIL_CACHE_SECONDS:
        return cached

    candles: dict[datetime, Candle] = {}
    cursor = start
    with BybitPublicClient() as client:
        while cursor <= end:
            page_end = min(end, cursor + ONE_MINUTE * (MAX_KLINE_LIMIT - 1))
            limit = int((page_end - cursor) / ONE_MINUTE) + 1
            for candle in client.get_klines(
                symbol, "1", limit=limit, start=cursor, end=page_end
            ):
                opened_at = candle.open_time.astimezone(UTC)
                if cursor <= opened_at <= page_end:
                    candles[opened_at] = candle
            cursor = page_end + ONE_MINUTE
        open_interest = (
            client.get_historical_open_interest(
                symbol,
                OPEN_INTEREST_INTERVALS[timeframe],
                start=start,
                end=end,
            )
            if end > start
            else []
        )
    result = _CachedTail(
        fetched_at=monotonic(),
        candles=[candles[timestamp] for timestamp in sorted(candles)],
        open_interest=open_interest,
    )
    _rest_tail_cache[key] = result
    return result


def _aggregate_rest_candles(
    candles: list[Candle], *, seconds: int
) -> list[dict[str, float | int]]:
    """Aggregate Bybit one-minute candles into the dashboard timeframe."""
    buckets: dict[int, list[Candle]] = {}
    for candle in candles:
        opened_at = int(candle.open_time.astimezone(UTC).timestamp())
        buckets.setdefault(opened_at // seconds * seconds, []).append(candle)
    return [
        {
            "time": bucket,
            "open": float(group[0].open),
            "high": float(max(candle.high for candle in group)),
            "low": float(min(candle.low for candle in group)),
            "close": float(group[-1].close),
            "volume": float(sum(candle.volume for candle in group)),
        }
        for bucket, group in sorted(buckets.items())
    ]


def _load_candles(
    connection: sqlite3.Connection,
    *,
    symbol: str,
    seconds: int,
    before: int | None,
) -> tuple[list[dict[str, float | int]], bool]:
    source_end = _candle_source_end(
        connection, symbol=symbol, seconds=seconds, before=before
    )
    if source_end is None:
        return [], False
    source_start = source_end - (CHART_CANDLE_LIMIT + 1) * seconds
    rows = connection.execute(
        """
        WITH source AS (
            SELECT CAST(strftime('%s', open_at) AS INTEGER) AS opened_at,
                   open, high, low, close, volume
            FROM historical_candles
            WHERE exchange = 'BYBIT' AND symbol = ? AND interval = '1'
              AND open_at >= ? AND open_at < ?
        ), ranked AS (
            SELECT (opened_at / ?) * ? AS bucket,
                   open, high, low, close, volume,
                   row_number() OVER (
                       PARTITION BY (opened_at / ?) * ? ORDER BY opened_at
                   ) AS first_row,
                   row_number() OVER (
                       PARTITION BY (opened_at / ?) * ? ORDER BY opened_at DESC
                   ) AS last_row
            FROM source
        ), aggregated AS (
            SELECT bucket,
                   MAX(CASE WHEN first_row = 1 THEN open END) AS open,
                   MAX(CAST(high AS REAL)) AS high,
                   MIN(CAST(low AS REAL)) AS low,
                   MAX(CASE WHEN last_row = 1 THEN close END) AS close,
                   SUM(CAST(volume AS REAL)) AS volume
            FROM ranked
            GROUP BY bucket
        ), bounded AS (
            SELECT bucket, open, high, low, close, volume
            FROM aggregated
            ORDER BY bucket DESC
            LIMIT ?
        )
        SELECT bucket, open, high, low, close, volume FROM bounded ORDER BY bucket
        """,
        (
            symbol,
            _utc_timestamp(source_start),
            _utc_timestamp(source_end),
            seconds,
            seconds,
            seconds,
            seconds,
            seconds,
            seconds,
            CHART_CANDLE_LIMIT + 1,
        ),
    ).fetchall()
    has_older = len(rows) > CHART_CANDLE_LIMIT and _has_older_candle_source_rows(
        connection, symbol=symbol, source_start=source_start
    )
    if has_older:
        rows = rows[1:]
    return [
        {
            "time": int(row["bucket"]),
            "open": float(row["open"]),
            "high": float(row["high"]),
            "low": float(row["low"]),
            "close": float(row["close"]),
            "volume": float(row["volume"]),
        }
        for row in rows
    ], has_older


def _load_detailed_candles(
    connections: Sequence[sqlite3.Connection],
    *,
    symbol: str,
    seconds: int,
    before: int | None,
    after: int | None,
) -> tuple[list[dict[str, float | int]], bool, bool]:
    """Load one bounded page from ordered detailed sources.

    Later connections win duplicate minute timestamps.  This makes archive
    replacement deterministic before timeframe aggregation, so an overlap can
    neither double volume nor create duplicate chart buckets.
    """
    if after is None:
        source_end = _detailed_candle_source_end(
            connections, symbol=symbol, seconds=seconds, before=before
        )
        if source_end is None:
            return [], False, False
        source_start = source_end - (CHART_CANDLE_LIMIT + 1) * seconds
    else:
        source_start = _floor_to_bucket(after, seconds) + seconds
        source_end = source_start + (CHART_CANDLE_LIMIT + 1) * seconds
    rows, has_older = _load_detailed_candle_source_range(
        connections,
        symbol=symbol,
        seconds=seconds,
        source_start=source_start,
        source_end=source_end,
        minimum=None,
        maximum=None,
    )
    if after is None and len(rows) > CHART_CANDLE_LIMIT:
        rows = rows[-CHART_CANDLE_LIMIT:]
        has_older = True
    elif after is not None and len(rows) > CHART_CANDLE_LIMIT:
        rows = rows[:CHART_CANDLE_LIMIT]
    has_newer = bool(rows) and _has_detailed_candle_source_rows(
        connections,
        symbol=symbol,
        source_start=int(rows[-1]["time"]) + seconds,
        minimum=None,
        maximum=None,
    )
    return rows, has_older, has_newer


def _load_candles_with_hourly_history(
    detailed_connections: Sequence[sqlite3.Connection],
    hourly_connection: sqlite3.Connection,
    *,
    symbol: str,
    seconds: int,
    before: int | None,
    after: int | None,
) -> tuple[list[dict[str, float | int]], bool, bool]:
    """Page chart buckets from detailed history, then the older hourly source."""
    if after is None:
        source_end = _combined_candle_source_end(
            detailed_connections,
            hourly_connection,
            symbol=symbol,
            seconds=seconds,
            before=before,
        )
        if source_end is None:
            return [], False, False
        source_start = source_end - (CHART_CANDLE_LIMIT + 1) * seconds
    else:
        source_start = _floor_to_bucket(after, seconds) + seconds
        source_end = source_start + (CHART_CANDLE_LIMIT + 1) * seconds
    detailed_rows, detailed_has_older = _load_detailed_candle_source_range(
        detailed_connections,
        symbol=symbol,
        seconds=seconds,
        source_start=max(source_start, HOURLY_HISTORY_BOUNDARY_TIMESTAMP),
        source_end=source_end,
        minimum=HOURLY_HISTORY_BOUNDARY_TIMESTAMP,
        maximum=None,
    )
    hourly_rows, hourly_has_older = _load_candle_source_range(
        hourly_connection,
        symbol=symbol,
        interval="60",
        seconds=seconds,
        source_start=source_start,
        source_end=min(source_end, HOURLY_HISTORY_BOUNDARY_TIMESTAMP),
        minimum=None,
        maximum=HOURLY_HISTORY_BOUNDARY_TIMESTAMP,
    )
    merged = {int(row["time"]): row for row in hourly_rows}
    merged.update({int(row["time"]): row for row in detailed_rows})
    candles = [merged[timestamp] for timestamp in sorted(merged)]
    has_older = (
        len(candles) > CHART_CANDLE_LIMIT or detailed_has_older or hourly_has_older
    )
    if after is None and len(candles) > CHART_CANDLE_LIMIT:
        candles = candles[-CHART_CANDLE_LIMIT:]
    elif after is not None and len(candles) > CHART_CANDLE_LIMIT:
        candles = candles[:CHART_CANDLE_LIMIT]
    has_newer = bool(candles) and (
        _has_detailed_candle_source_rows(
            detailed_connections,
            symbol=symbol,
            source_start=int(candles[-1]["time"]) + seconds,
            minimum=HOURLY_HISTORY_BOUNDARY_TIMESTAMP,
            maximum=None,
        )
        or _has_candle_source_rows(
            hourly_connection,
            symbol=symbol,
            interval="60",
            source_start=int(candles[-1]["time"]) + seconds,
            minimum=None,
            maximum=HOURLY_HISTORY_BOUNDARY_TIMESTAMP,
        )
    )
    return candles, has_older, has_newer


def _combined_candle_source_end(
    detailed_connections: Sequence[sqlite3.Connection],
    hourly_connection: sqlite3.Connection,
    *,
    symbol: str,
    seconds: int,
    before: int | None,
) -> int | None:
    if before is not None:
        return _ceil_to_bucket(before, seconds)
    ends = [
        _detailed_candle_source_end(
            detailed_connections, symbol=symbol, seconds=seconds, before=None
        ),
        _candle_source_end_for_interval(
            hourly_connection, symbol=symbol, interval="60", seconds=seconds
        ),
    ]
    return max((end for end in ends if end is not None), default=None)


def _candle_source_end_for_interval(
    connection: sqlite3.Connection, *, symbol: str, interval: str, seconds: int
) -> int | None:
    row = connection.execute(
        """
        SELECT open_at
        FROM historical_candles
        WHERE exchange = 'BYBIT' AND symbol = ? AND interval = ?
        ORDER BY open_at DESC
        LIMIT 1
        """,
        (symbol, interval),
    ).fetchone()
    if row is None:
        return None
    newest = int(datetime.fromisoformat(row["open_at"]).astimezone(UTC).timestamp())
    return newest // seconds * seconds + seconds


def _detailed_candle_source_end(
    connections: Sequence[sqlite3.Connection],
    *,
    symbol: str,
    seconds: int,
    before: int | None,
) -> int | None:
    if before is not None:
        return _ceil_to_bucket(before, seconds)
    ends = [
        _candle_source_end(connection, symbol=symbol, seconds=seconds, before=None)
        for connection in connections
    ]
    return max((end for end in ends if end is not None), default=None)


def _load_detailed_candle_source_range(
    connections: Sequence[sqlite3.Connection],
    *,
    symbol: str,
    seconds: int,
    source_start: int,
    source_end: int,
    minimum: int | None,
    maximum: int | None,
) -> tuple[list[dict[str, float | int]], bool]:
    """Merge bounded raw minutes from ordered sources, then aggregate them."""
    if source_end <= source_start:
        return [], False
    source_rows: dict[int, sqlite3.Row] = {}
    has_older = False
    for connection in connections:
        conditions = [
            "exchange = 'BYBIT'",
            "symbol = ?",
            "interval = '1'",
            "open_at >= ?",
            "open_at < ?",
        ]
        parameters: list[object] = [
            symbol, _utc_timestamp(source_start), _utc_timestamp(source_end)
        ]
        if minimum is not None:
            conditions.append("open_at >= ?")
            parameters.append(_utc_timestamp(minimum))
        if maximum is not None:
            conditions.append("open_at < ?")
            parameters.append(_utc_timestamp(maximum))
        where = " AND ".join(conditions)
        rows = connection.execute(
            f"""
            SELECT CAST(strftime('%s', open_at) AS INTEGER) AS opened_at,
                   open, high, low, close, volume
            FROM historical_candles
            WHERE {where}
            ORDER BY open_at
            """,
            parameters,
        ).fetchall()
        for row in rows:
            source_rows[int(row["opened_at"])] = row
        older_conditions = [
            "exchange = 'BYBIT'",
            "symbol = ?",
            "interval = '1'",
            "open_at < ?",
        ]
        older_parameters: list[object] = [symbol, _utc_timestamp(source_start)]
        if minimum is not None:
            older_conditions.append("open_at >= ?")
            older_parameters.append(_utc_timestamp(minimum))
        if maximum is not None:
            older_conditions.append("open_at < ?")
            older_parameters.append(_utc_timestamp(maximum))
        older_where = " AND ".join(older_conditions)
        has_older |= bool(
            connection.execute(
                f"SELECT EXISTS(SELECT 1 FROM historical_candles WHERE {older_where})",
                older_parameters,
            ).fetchone()[0]
        )
    buckets: dict[int, list[tuple[int, sqlite3.Row]]] = {}
    for opened_at, row in source_rows.items():
        buckets.setdefault(opened_at // seconds * seconds, []).append((opened_at, row))
    return [
        {
            "time": bucket,
            "open": float(sorted_group[0][1]["open"]),
            "high": max(float(row["high"]) for _, row in sorted_group),
            "low": min(float(row["low"]) for _, row in sorted_group),
            "close": float(sorted_group[-1][1]["close"]),
            "volume": sum(float(row["volume"]) for _, row in sorted_group),
        }
        for bucket, group in sorted(buckets.items())
        for sorted_group in [sorted(group)]
    ], has_older


def _has_detailed_candle_source_rows(
    connections: Sequence[sqlite3.Connection],
    *,
    symbol: str,
    source_start: int,
    minimum: int | None,
    maximum: int | None,
) -> bool:
    """Return whether any ordered detailed source has a later raw candle."""
    for connection in connections:
        conditions = [
            "exchange = 'BYBIT'",
            "symbol = ?",
            "interval = '1'",
            "open_at >= ?",
        ]
        parameters: list[object] = [symbol, _utc_timestamp(source_start)]
        if minimum is not None:
            conditions.append("open_at >= ?")
            parameters.append(_utc_timestamp(minimum))
        if maximum is not None:
            conditions.append("open_at < ?")
            parameters.append(_utc_timestamp(maximum))
        where = " AND ".join(conditions)
        if connection.execute(
            f"SELECT EXISTS(SELECT 1 FROM historical_candles WHERE {where})",
            parameters,
        ).fetchone()[0]:
            return True
    return False


def _load_candle_source_range(
    connection: sqlite3.Connection,
    *,
    symbol: str,
    interval: str,
    seconds: int,
    source_start: int,
    source_end: int,
    minimum: int | None,
    maximum: int | None,
) -> tuple[list[dict[str, float | int]], bool]:
    """Aggregate one source over its allowed time range without crossing it."""
    if source_end <= source_start:
        return [], False
    conditions = [
        "exchange = 'BYBIT'",
        "symbol = ?",
        "interval = ?",
        "open_at >= ?",
        "open_at < ?",
    ]
    parameters: list[object] = [
        symbol,
        interval,
        _utc_timestamp(source_start),
        _utc_timestamp(source_end),
    ]
    if minimum is not None:
        conditions.append("open_at >= ?")
        parameters.append(_utc_timestamp(minimum))
    if maximum is not None:
        conditions.append("open_at < ?")
        parameters.append(_utc_timestamp(maximum))
    where = " AND ".join(conditions)
    rows = connection.execute(
        f"""
        WITH source AS (
            SELECT CAST(strftime('%s', open_at) AS INTEGER) AS opened_at,
                   open, high, low, close, volume
            FROM historical_candles
            WHERE {where}
        ), ranked AS (
            SELECT (opened_at / ?) * ? AS bucket,
                   open, high, low, close, volume,
                   row_number() OVER (
                       PARTITION BY (opened_at / ?) * ? ORDER BY opened_at
                   ) AS first_row,
                   row_number() OVER (
                       PARTITION BY (opened_at / ?) * ? ORDER BY opened_at DESC
                   ) AS last_row
            FROM source
        )
        SELECT bucket,
               MAX(CASE WHEN first_row = 1 THEN open END) AS open,
               MAX(CAST(high AS REAL)) AS high,
               MIN(CAST(low AS REAL)) AS low,
               MAX(CASE WHEN last_row = 1 THEN close END) AS close,
               SUM(CAST(volume AS REAL)) AS volume
        FROM ranked
        GROUP BY bucket
        ORDER BY bucket
        """,
        (*parameters, seconds, seconds, seconds, seconds, seconds, seconds),
    ).fetchall()
    older_conditions = [
        "exchange = 'BYBIT'",
        "symbol = ?",
        "interval = ?",
        "open_at < ?",
    ]
    older_parameters: list[object] = [symbol, interval, _utc_timestamp(source_start)]
    if minimum is not None:
        older_conditions.append("open_at >= ?")
        older_parameters.append(_utc_timestamp(minimum))
    if maximum is not None:
        older_conditions.append("open_at < ?")
        older_parameters.append(_utc_timestamp(maximum))
    older_where = " AND ".join(older_conditions)
    has_older = connection.execute(
        f"SELECT EXISTS(SELECT 1 FROM historical_candles WHERE {older_where})",
        older_parameters,
    ).fetchone()[0]
    return [
        {
            "time": int(row["bucket"]),
            "open": float(row["open"]),
            "high": float(row["high"]),
            "low": float(row["low"]),
            "close": float(row["close"]),
            "volume": float(row["volume"]),
        }
        for row in rows
    ], bool(has_older)


def _has_candle_source_rows(
    connection: sqlite3.Connection,
    *,
    symbol: str,
    interval: str,
    source_start: int,
    minimum: int | None,
    maximum: int | None,
) -> bool:
    conditions = [
        "exchange = 'BYBIT'",
        "symbol = ?",
        "interval = ?",
        "open_at >= ?",
    ]
    parameters: list[object] = [symbol, interval, _utc_timestamp(source_start)]
    if minimum is not None:
        conditions.append("open_at >= ?")
        parameters.append(_utc_timestamp(minimum))
    if maximum is not None:
        conditions.append("open_at < ?")
        parameters.append(_utc_timestamp(maximum))
    where = " AND ".join(conditions)
    return bool(
        connection.execute(
            f"SELECT EXISTS(SELECT 1 FROM historical_candles WHERE {where})",
            parameters,
        ).fetchone()[0]
    )


def _candle_source_end(
    connection: sqlite3.Connection,
    *,
    symbol: str,
    seconds: int,
    before: int | None,
) -> int | None:
    """Return the exclusive UTC bucket boundary for one chart page."""
    if before is not None:
        return _ceil_to_bucket(before, seconds)
    row = connection.execute(
        """
        SELECT open_at
        FROM historical_candles
        WHERE exchange = 'BYBIT' AND symbol = ? AND interval = '1'
        ORDER BY open_at DESC
        LIMIT 1
        """,
        (symbol,),
    ).fetchone()
    if row is None:
        return None
    newest = int(datetime.fromisoformat(row["open_at"]).astimezone(UTC).timestamp())
    return newest // seconds * seconds + seconds


def _has_older_candle_source_rows(
    connection: sqlite3.Connection, *, symbol: str, source_start: int
) -> bool:
    row = connection.execute(
        """
        SELECT EXISTS(
            SELECT 1
            FROM historical_candles
            WHERE exchange = 'BYBIT' AND symbol = ? AND interval = '1'
              AND open_at < ?
        )
        """,
        (symbol, _utc_timestamp(source_start)),
    ).fetchone()
    return row is not None and int(row[0]) == 1


def _ceil_to_bucket(timestamp: int, seconds: int) -> int:
    return -(-timestamp // seconds) * seconds


def _floor_to_bucket(timestamp: int, seconds: int) -> int:
    return timestamp // seconds * seconds


def _utc_timestamp(timestamp: int) -> str:
    return datetime.fromtimestamp(timestamp, UTC).isoformat()


def _load_open_interest(
    connection: sqlite3.Connection,
    *,
    symbol: str,
    seconds: int,
    source_interval: str,
    start: int | None,
    end: int | None,
) -> list[dict[str, float | int]]:
    """Return the final native OI observation in each chart-timeframe bucket."""
    if start is None or end is None:
        return []
    rows = connection.execute(
        """
        WITH source AS (
            SELECT CAST(strftime('%s', timestamp) AS INTEGER) AS observed_at,
                   open_interest
            FROM historical_open_interest
            WHERE exchange = 'BYBIT' AND symbol = ? AND source_interval = ?
              AND timestamp BETWEEN ? AND ?
        ), ranked AS (
            SELECT (observed_at / ?) * ? AS bucket,
                   open_interest,
                   row_number() OVER (
                       PARTITION BY (observed_at / ?) * ? ORDER BY observed_at DESC
                   ) AS last_row
            FROM source
        )
        SELECT bucket, open_interest
        FROM ranked
        WHERE last_row = 1
        ORDER BY bucket
        """,
        (
            symbol,
            source_interval,
            _utc_timestamp(start),
            _utc_timestamp(end),
            seconds,
            seconds,
            seconds,
            seconds,
        ),
    ).fetchall()
    return [
        {"time": int(row["bucket"]), "value": float(row["open_interest"])}
        for row in rows
    ]


def _load_detailed_open_interest(
    connections: Sequence[sqlite3.Connection],
    *,
    symbol: str,
    seconds: int,
    source_interval: str,
    start: int | None,
    end: int | None,
) -> list[dict[str, float | int]]:
    """Merge OI observations from ordered detailed sources without fabrication."""
    if start is None or end is None:
        return []
    observations: dict[int, float] = {}
    for connection in connections:
        rows = connection.execute(
            """
            SELECT CAST(strftime('%s', timestamp) AS INTEGER) AS observed_at,
                   open_interest
            FROM historical_open_interest
            WHERE exchange = 'BYBIT' AND symbol = ? AND source_interval = ?
              AND timestamp BETWEEN ? AND ?
            ORDER BY timestamp
            """,
            (symbol, source_interval, _utc_timestamp(start), _utc_timestamp(end)),
        ).fetchall()
        for row in rows:
            observations[int(row["observed_at"])] = float(row["open_interest"])
    buckets: dict[int, tuple[int, float]] = {}
    for observed_at, value in observations.items():
        bucket = observed_at // seconds * seconds
        previous = buckets.get(bucket)
        if previous is None or observed_at > previous[0]:
            buckets[bucket] = (observed_at, value)
    return [
        {"time": bucket, "value": value}
        for bucket, (_, value) in sorted(buckets.items())
    ]


def _aggregate_open_interest(
    points: list[OpenInterestPoint], *, seconds: int
) -> list[dict[str, float | int]]:
    """Reduce native OI observations to the final observation in every bucket."""
    buckets: dict[int, OpenInterestPoint] = {}
    for point in points:
        observed_at = int(point.timestamp.astimezone(UTC).timestamp())
        buckets[observed_at // seconds * seconds] = point
    return [
        {"time": bucket, "value": float(point.open_interest)}
        for bucket, point in sorted(buckets.items())
    ]


def _markers_for_candles(
    events: list[SignalEvent],
    *,
    seconds: int,
    candle_times: set[int],
    marker_labels: dict[str, str] | None = None,
) -> list[dict[str, int | str | None]]:
    labels = marker_labels or {
        item.event_type: item.short_label
        for item in PRODUCTION_SIGNAL_TYPE_REGISTRY.signal_types
    }
    markers: list[dict[str, int | str | None]] = []
    for event in events:
        event_time = _event_timestamp(event.event_at)
        candle_time = event_time // seconds * seconds
        if candle_time not in candle_times:
            continue
        marker_type = labels.get(event.event_type, event.event_type)
        markers.append(
            {
                "time": candle_time,
                "type": marker_type,
                "eventId": event.event_id,
                "eventType": event.event_type,
                "eventAt": event.event_at,
                "eventPrice": event.event_price,
                "sourceInterval": event.source_interval or "—",
                "alertLevel": event.alert_level or "—",
            }
        )
    return sorted(
        markers,
        key=lambda marker: (cast(int, marker["time"]), str(marker["type"])),
    )


def _event_timestamp(value: str) -> int:
    event_at = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return int(event_at.astimezone(UTC).timestamp())


def _detailed_history_boundary_metadata() -> dict[str, int | str]:
    return {
        "time": HOURLY_HISTORY_BOUNDARY_TIMESTAMP,
        "label": "Earlier history available on 1h+",
    }


@contextmanager
def _readonly_connection(database_path: Path) -> Iterator[sqlite3.Connection]:
    if not database_path.exists():
        raise FileNotFoundError(database_path)
    connection = sqlite3.connect(f"file:{database_path.absolute()}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    try:
        connection.execute("PRAGMA query_only = ON")
        yield connection
    finally:
        connection.close()


@contextmanager
def _readonly_connections(
    database_paths: Sequence[Path],
) -> Iterator[tuple[sqlite3.Connection, ...]]:
    """Open available detailed sources in oldest-to-newest precedence order."""
    with ExitStack() as stack:
        connections = tuple(
            stack.enter_context(_readonly_connection(path))
            for path in database_paths
            if path.exists()
        )
        if not connections:
            raise FileNotFoundError(database_paths[-1])
        yield connections


@contextmanager
def _optional_readonly_connection(
    database_path: Path | None,
) -> Iterator[sqlite3.Connection | None]:
    if database_path is None or not database_path.exists():
        yield None
        return
    with _readonly_connection(database_path) as connection:
        yield connection
