"""Client for Binance USDⓈ-M public market-data endpoints."""

from __future__ import annotations

import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import httpx

from dashboard.exchanges.models import Candle, MarketSymbol, OpenInterestPoint

BASE_URL = "https://fapi.binance.com"
EXCHANGE = "BINANCE"
DEFAULT_MAX_ATTEMPTS = 4
FALLBACK_RETRY_DELAY = 1.0
MAX_RETRY_DELAY = 60.0
MAX_KLINE_LIMIT = 1_500
MAX_OPEN_INTEREST_LIMIT = 500
MAX_KLINE_PAGES = 2
MAX_OPEN_INTEREST_PAGES = 4
HISTORICAL_OPEN_INTEREST_AVAILABILITY = timedelta(days=30)
KLINE_INTERVALS = {
    "1m": timedelta(minutes=1),
    "3m": timedelta(minutes=3),
    "5m": timedelta(minutes=5),
    "15m": timedelta(minutes=15),
    "30m": timedelta(minutes=30),
    "1h": timedelta(hours=1),
    "2h": timedelta(hours=2),
    "4h": timedelta(hours=4),
    "6h": timedelta(hours=6),
    "8h": timedelta(hours=8),
    "12h": timedelta(hours=12),
    "1d": timedelta(days=1),
    "3d": timedelta(days=3),
    "1w": timedelta(days=7),
}
OPEN_INTEREST_INTERVALS = {
    "5m": timedelta(minutes=5),
    "15m": timedelta(minutes=15),
    "30m": timedelta(minutes=30),
    "1h": timedelta(hours=1),
    "2h": timedelta(hours=2),
    "4h": timedelta(hours=4),
    "6h": timedelta(hours=6),
    "12h": timedelta(hours=12),
    "1d": timedelta(days=1),
}


class BinanceAPIError(RuntimeError):
    """Raised when Binance returns an HTTP or API-level error."""


class BinanceRateLimitError(BinanceAPIError):
    """Raised when Binance rate limiting remains after bounded retries."""


class BinanceSymbolError(BinanceAPIError):
    """Raised when a symbol is not an active Binance USDⓈ-M perpetual."""


@dataclass(frozen=True, slots=True)
class BinanceCurrentOpenInterest:
    """Raw current open interest reported by Binance for one USDⓈ-M symbol."""

    exchange: str
    symbol: str
    open_interest: Decimal
    observed_at: datetime


@dataclass(frozen=True, slots=True)
class BinanceMarkPrice:
    """Current Binance mark price and funding metadata for one symbol."""

    exchange: str
    symbol: str
    mark_price: Decimal
    last_funding_rate: Decimal
    next_funding_time: datetime
    observed_at: datetime


@dataclass(frozen=True, slots=True)
class BinanceTicker24h:
    """Current Binance USDⓈ-M 24-hour ticker values for one symbol."""

    exchange: str
    symbol: str
    last_price: Decimal
    price_change: Decimal
    price_change_percent: Decimal
    high_price: Decimal
    low_price: Decimal
    quote_volume: Decimal


class BinanceUsdmPublicClient:
    """Read public Binance USDⓈ-M Futures market data without authentication."""

    def __init__(
        self,
        *,
        timeout: float = 10.0,
        transport: httpx.BaseTransport | None = None,
        max_attempts: int = DEFAULT_MAX_ATTEMPTS,
        sleeper: Callable[[float], None] = time.sleep,
    ) -> None:
        if max_attempts < 1:
            raise ValueError("max_attempts must be at least 1")
        self._client = httpx.Client(
            base_url=BASE_URL, timeout=timeout, transport=transport
        )
        self._max_attempts = max_attempts
        self._sleeper = sleeper

    def __enter__(self) -> BinanceUsdmPublicClient:
        return self

    def __exit__(self, *args: object) -> None:
        self.close()

    def close(self) -> None:
        self._client.close()

    def get_klines(
        self,
        symbol: str,
        interval: str,
        limit: int = 500,
        *,
        start: datetime | None = None,
        end: datetime | None = None,
    ) -> list[Candle]:
        """Return one bounded Binance USDⓈ-M kline page, oldest first."""
        _validate_symbol(symbol)
        if interval not in KLINE_INTERVALS and interval != "1M":
            raise ValueError(f"unsupported Binance kline interval: {interval}")
        if not 1 <= limit <= MAX_KLINE_LIMIT:
            raise ValueError(f"limit must be between 1 and {MAX_KLINE_LIMIT}")
        params: dict[str, str | int] = {
            "symbol": symbol,
            "interval": interval,
            "limit": limit,
        }
        if start is not None:
            params["startTime"] = _timestamp_milliseconds(start)
        if end is not None:
            params["endTime"] = _timestamp_milliseconds(end)
        payload = self._get("/fapi/v1/klines", params)
        if not isinstance(payload, list):
            raise BinanceAPIError("Binance returned an invalid kline response")
        return _candles(payload, symbol=symbol, interval=interval)

    def get_historical_klines(
        self,
        symbol: str,
        interval: str,
        *,
        start: datetime,
        end: datetime,
        page_limit: int = MAX_KLINE_LIMIT,
        max_pages: int = MAX_KLINE_PAGES,
    ) -> list[Candle]:
        """Return a bounded kline range without unbounded pagination."""
        _validate_kline_interval(interval)
        _validate_page_arguments(
            start=start,
            end=end,
            page_limit=page_limit,
            max_limit=MAX_KLINE_LIMIT,
            max_pages=max_pages,
            label="kline",
        )
        candles: dict[datetime, Candle] = {}
        cursor = start.astimezone(UTC)
        for _ in range(max_pages):
            page = self.get_klines(
                symbol,
                interval,
                limit=page_limit,
                start=cursor,
                end=end,
            )
            for candle in page:
                if start <= candle.open_time <= end:
                    candles[candle.open_time] = candle
            if len(page) < page_limit:
                break
            last_open = page[-1].open_time
            next_cursor = _next_kline_cursor(last_open, interval)
            if next_cursor <= cursor:
                raise BinanceAPIError("Binance returned non-advancing kline page")
            cursor = next_cursor
            if cursor > end:
                break
        return [candles[timestamp] for timestamp in sorted(candles)]

    def get_historical_open_interest(
        self,
        symbol: str,
        period: str,
        *,
        start: datetime,
        end: datetime,
        page_limit: int = MAX_OPEN_INTEREST_LIMIT,
        max_pages: int = MAX_OPEN_INTEREST_PAGES,
        now: datetime | None = None,
    ) -> list[OpenInterestPoint]:
        """Return bounded Binance OI value samples from its latest 30-day window."""
        _validate_symbol(symbol)
        period_delta = OPEN_INTEREST_INTERVALS.get(period)
        if period_delta is None:
            raise ValueError(f"unsupported Binance open-interest period: {period}")
        _validate_page_arguments(
            start=start,
            end=end,
            page_limit=page_limit,
            max_limit=MAX_OPEN_INTEREST_LIMIT,
            max_pages=max_pages,
            label="open-interest",
        )
        current_time = datetime.now(UTC) if now is None else _as_utc(now)
        if start < current_time - HISTORICAL_OPEN_INTEREST_AVAILABILITY:
            raise ValueError(
                "Binance historical open interest is only available for the latest "
                "30 days"
            )

        points: dict[datetime, OpenInterestPoint] = {}
        cursor = start.astimezone(UTC)
        for _ in range(max_pages):
            payload = self._get(
                "/futures/data/openInterestHist",
                {
                    "symbol": symbol,
                    "period": period,
                    "limit": page_limit,
                    "startTime": _timestamp_milliseconds(cursor),
                    "endTime": _timestamp_milliseconds(end),
                },
            )
            if not isinstance(payload, list):
                raise BinanceAPIError(
                    "Binance returned an invalid historical open-interest response"
                )
            page = _historical_open_interest_points(
                payload, symbol=symbol, period=period
            )
            for point in page:
                if start <= point.timestamp <= end:
                    points[point.timestamp] = point
            if len(page) < page_limit:
                break
            next_cursor = page[-1].timestamp + period_delta
            if next_cursor <= cursor:
                raise BinanceAPIError(
                    "Binance returned non-advancing historical open-interest page"
                )
            cursor = next_cursor
            if cursor > end:
                break
        return [points[timestamp] for timestamp in sorted(points)]

    def get_current_open_interest(self, symbol: str) -> BinanceCurrentOpenInterest:
        """Return raw current Binance open interest without presentation conversion."""
        _validate_symbol(symbol)
        payload = self._get("/fapi/v1/openInterest", {"symbol": symbol})
        if not isinstance(payload, dict):
            raise BinanceAPIError(
                "Binance returned an invalid current open-interest response"
            )
        try:
            return BinanceCurrentOpenInterest(
                exchange=EXCHANGE,
                symbol=_required_string(payload, "symbol"),
                open_interest=Decimal(_required_string(payload, "openInterest")),
                observed_at=_timestamp(_required_integer(payload, "time")),
            )
        except (ArithmeticError, ValueError) as exc:
            raise BinanceAPIError(
                "Binance returned an invalid current open-interest response"
            ) from exc

    def get_mark_price(self, symbol: str) -> BinanceMarkPrice:
        """Return current mark price and funding metadata for a Binance symbol."""
        _validate_symbol(symbol)
        payload = self._get("/fapi/v1/premiumIndex", {"symbol": symbol})
        if not isinstance(payload, dict):
            raise BinanceAPIError("Binance returned an invalid mark-price response")
        try:
            return BinanceMarkPrice(
                exchange=EXCHANGE,
                symbol=_required_string(payload, "symbol"),
                mark_price=Decimal(_required_string(payload, "markPrice")),
                last_funding_rate=Decimal(
                    _required_string(payload, "lastFundingRate")
                ),
                next_funding_time=_timestamp(
                    _required_integer(payload, "nextFundingTime")
                ),
                observed_at=_timestamp(_required_integer(payload, "time")),
            )
        except (ArithmeticError, ValueError) as exc:
            raise BinanceAPIError(
                "Binance returned an invalid mark-price response"
            ) from exc

    def get_24h_ticker(self, symbol: str) -> BinanceTicker24h:
        """Return current Binance USDⓈ-M 24-hour ticker values for one symbol."""
        _validate_symbol(symbol)
        payload = self._get("/fapi/v1/ticker/24hr", {"symbol": symbol})
        if not isinstance(payload, dict):
            raise BinanceAPIError("Binance returned an invalid 24-hour ticker response")
        try:
            return BinanceTicker24h(
                exchange=EXCHANGE,
                symbol=_required_string(payload, "symbol"),
                last_price=Decimal(_required_string(payload, "lastPrice")),
                price_change=Decimal(_required_string(payload, "priceChange")),
                price_change_percent=Decimal(
                    _required_string(payload, "priceChangePercent")
                ),
                high_price=Decimal(_required_string(payload, "highPrice")),
                low_price=Decimal(_required_string(payload, "lowPrice")),
                quote_volume=Decimal(_required_string(payload, "quoteVolume")),
            )
        except (ArithmeticError, ValueError) as exc:
            raise BinanceAPIError(
                "Binance returned an invalid 24-hour ticker response"
            ) from exc

    def get_usdm_perpetual_symbol(self, symbol: str) -> MarketSymbol:
        """Validate and return one active Binance USDⓈ-M perpetual symbol."""
        _validate_symbol(symbol)
        payload = self._get("/fapi/v1/exchangeInfo", {})
        if not isinstance(payload, dict):
            raise BinanceAPIError("Binance returned an invalid exchange-info response")
        symbols = payload.get("symbols")
        if not isinstance(symbols, list):
            raise BinanceAPIError("Binance returned an invalid exchange-info response")
        item = next(
            (
                candidate
                for candidate in symbols
                if isinstance(candidate, dict) and candidate.get("symbol") == symbol
            ),
            None,
        )
        if item is None:
            raise BinanceSymbolError(f"Binance USDⓈ-M symbol not found: {symbol}")
        if item.get("contractType") != "PERPETUAL":
            raise BinanceSymbolError(
                f"Binance USDⓈ-M symbol is not perpetual: {symbol}"
            )
        if item.get("status") != "TRADING":
            raise BinanceSymbolError(f"Binance USDⓈ-M symbol is not trading: {symbol}")
        try:
            return MarketSymbol(
                exchange=EXCHANGE,
                symbol=symbol,
                base_asset=_required_string(item, "baseAsset"),
                quote_asset=_required_string(item, "quoteAsset"),
                market_type="perpetual",
                is_active=True,
            )
        except ValueError as exc:
            raise BinanceAPIError(
                "Binance returned an invalid exchange-info response"
            ) from exc

    def _get(self, path: str, params: Mapping[str, str | int]) -> object:
        for attempt in range(self._max_attempts):
            try:
                response = self._client.get(path, params=params)
            except httpx.HTTPError as exc:
                raise BinanceAPIError(f"Binance request failed: {exc}") from exc
            if response.status_code == 429:
                if attempt + 1 == self._max_attempts:
                    raise BinanceRateLimitError("Binance request rate limit exceeded")
                self._sleeper(_retry_delay(response, attempt))
                continue
            try:
                response.raise_for_status()
            except httpx.HTTPError as exc:
                raise BinanceAPIError(_error_message(response)) from exc
            try:
                payload = response.json()
            except ValueError as exc:
                raise BinanceAPIError("Binance returned invalid JSON") from exc
            if isinstance(payload, dict) and payload.get("code", 0) != 0:
                raise BinanceAPIError(_binance_error_message(payload))
            return payload
        raise AssertionError("retry loop completed unexpectedly")


def _candles(payload: list[object], *, symbol: str, interval: str) -> list[Candle]:
    candles: list[Candle] = []
    try:
        for row in payload:
            if not isinstance(row, list) or len(row) < 8:
                raise ValueError("invalid kline row")
            candles.append(
                Candle(
                    exchange=EXCHANGE,
                    symbol=symbol,
                    interval=interval,
                    open_time=_timestamp(row[0]),
                    open=Decimal(str(row[1])),
                    high=Decimal(str(row[2])),
                    low=Decimal(str(row[3])),
                    close=Decimal(str(row[4])),
                    volume=Decimal(str(row[5])),
                    turnover=Decimal(str(row[7])),
                )
            )
    except (ArithmeticError, TypeError, ValueError) as exc:
        raise BinanceAPIError("Binance returned an invalid kline response") from exc
    return sorted(candles, key=lambda candle: candle.open_time)


def _historical_open_interest_points(
    payload: list[object], *, symbol: str, period: str
) -> list[OpenInterestPoint]:
    """Map Binance's documented total OI value without filling omitted periods."""
    points: list[OpenInterestPoint] = []
    try:
        for item in payload:
            if not isinstance(item, dict):
                raise ValueError("invalid historical open-interest item")
            points.append(
                OpenInterestPoint(
                    exchange=EXCHANGE,
                    symbol=symbol,
                    interval=period,
                    timestamp=_timestamp(_required_integer(item, "timestamp")),
                    open_interest=Decimal(
                        _required_string(item, "sumOpenInterestValue")
                    ),
                )
            )
    except (ArithmeticError, TypeError, ValueError) as exc:
        raise BinanceAPIError(
            "Binance returned an invalid historical open-interest response"
        ) from exc
    return sorted(points, key=lambda point: point.timestamp)


def _validate_symbol(symbol: str) -> None:
    if not symbol or symbol != symbol.upper() or not symbol.isalnum():
        raise ValueError("Binance REST symbols must be uppercase alphanumeric strings")


def _validate_kline_interval(interval: str) -> None:
    if interval == "1M":
        return
    try:
        KLINE_INTERVALS[interval]
    except KeyError as exc:
        raise ValueError(f"unsupported Binance kline interval: {interval}") from exc


def _next_kline_cursor(open_time: datetime, interval: str) -> datetime:
    if interval != "1M":
        return open_time + KLINE_INTERVALS[interval]
    if open_time.month == 12:
        return open_time.replace(year=open_time.year + 1, month=1)
    return open_time.replace(month=open_time.month + 1)


def _validate_page_arguments(
    *,
    start: datetime,
    end: datetime,
    page_limit: int,
    max_limit: int,
    max_pages: int,
    label: str,
) -> None:
    start_utc = _as_utc(start)
    end_utc = _as_utc(end)
    if end_utc <= start_utc:
        raise ValueError(f"{label} end must be later than start")
    if not 1 <= page_limit <= max_limit:
        raise ValueError(f"{label} page_limit must be between 1 and {max_limit}")
    if max_pages < 1:
        raise ValueError(f"{label} max_pages must be at least 1")


def _timestamp(value: object) -> datetime:
    return datetime.fromtimestamp(int(value) / 1_000, tz=UTC)


def _timestamp_milliseconds(value: datetime) -> int:
    return int(_as_utc(value).timestamp() * 1_000)


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("datetime must be timezone-aware")
    return value.astimezone(UTC)


def _required_string(payload: Mapping[str, object], name: str) -> str:
    value = payload.get(name)
    if not isinstance(value, str) or not value:
        raise ValueError(f"{name} is required")
    return value


def _required_integer(payload: Mapping[str, object], name: str) -> int:
    value = payload.get(name)
    if isinstance(value, bool):
        raise ValueError(f"{name} must be an integer")
    return int(value)


def _retry_delay(response: httpx.Response, attempt: int) -> float:
    retry_after = response.headers.get("Retry-After")
    if retry_after is not None:
        try:
            delay = float(retry_after)
        except ValueError:
            pass
        else:
            if 0 <= delay <= MAX_RETRY_DELAY:
                return delay
    return min(FALLBACK_RETRY_DELAY * 2**attempt, MAX_RETRY_DELAY)


def _error_message(response: httpx.Response) -> str:
    try:
        payload = response.json()
    except ValueError:
        return f"Binance request failed: HTTP {response.status_code}"
    if isinstance(payload, dict):
        return _binance_error_message(payload)
    return f"Binance request failed: HTTP {response.status_code}"


def _binance_error_message(payload: Mapping[str, object]) -> str:
    code = payload.get("code")
    message = payload.get("msg")
    if code is not None and isinstance(message, str):
        return f"Binance API error {code}: {message}"
    return "Binance request failed"
