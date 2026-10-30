"""Client for Bybit's public V5 market data endpoints."""

import time
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from decimal import Decimal
from threading import Lock
from typing import Any

import httpx

from dashboard.exchanges.models import (
    Candle,
    FundingRate,
    MarketSnapshot,
    MarketSymbol,
    OpenInterestPoint,
)

BASE_URL = "https://api.bybit.com"
EXCHANGE = "BYBIT"
DEFAULT_MAX_ATTEMPTS = 4
DEFAULT_REQUESTS_PER_SECOND = 10.0
FALLBACK_RETRY_DELAY = 1.0
MAX_RETRY_DELAY = 60.0
MAX_KLINE_LIMIT = 1_000
MAX_OPEN_INTEREST_LIMIT = 200
OPEN_INTEREST_INTERVALS = frozenset({"5min", "15min", "1h"})


class BybitAPIError(RuntimeError):
    """Raised when Bybit returns an HTTP or API-level error."""


class BybitRateLimitError(BybitAPIError):
    """Raised when Bybit rate limiting remains after bounded retries."""

    def __init__(self, code: int | str, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(f"Bybit API error {code}: {message}")


class BybitPublicClient:
    """Read public Bybit market data without authentication."""

    def __init__(
        self,
        *,
        timeout: float = 10.0,
        transport: httpx.BaseTransport | None = None,
        max_attempts: int = DEFAULT_MAX_ATTEMPTS,
        sleeper: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.time,
        monotonic_clock: Callable[[], float] = time.monotonic,
        requests_per_second: float = DEFAULT_REQUESTS_PER_SECOND,
    ) -> None:
        if max_attempts < 1:
            raise ValueError("max_attempts must be at least 1")
        if requests_per_second <= 0:
            raise ValueError("requests_per_second must be greater than 0")
        self._client = httpx.Client(
            base_url=BASE_URL, timeout=timeout, transport=transport
        )
        self._max_attempts = max_attempts
        self._sleeper = sleeper
        self._clock = clock
        self._monotonic_clock = monotonic_clock
        self._request_interval = 1.0 / requests_per_second
        self._rate_limit_lock = Lock()
        self._next_request_time = 0.0

    def __enter__(self) -> "BybitPublicClient":
        return self

    def __exit__(self, *args: object) -> None:
        self.close()

    def close(self) -> None:
        self._client.close()

    def _get(self, path: str, params: Mapping[str, str | int]) -> dict[str, Any]:
        for attempt in range(self._max_attempts):
            try:
                self._wait_for_request_slot()
                response = self._client.get(path, params=params)
            except httpx.HTTPError as exc:
                raise BybitAPIError(f"Bybit request failed: {exc}") from exc

            payload = _response_payload(response)
            code = payload.get("retCode") if payload is not None else None
            if response.status_code == 429 or code == 10006:
                message = (
                    str(payload.get("retMsg", "Too Many Requests"))
                    if payload is not None
                    else "Too Many Requests"
                )
                error_code: int | str = code if code is not None else 429
                if attempt + 1 == self._max_attempts:
                    raise BybitRateLimitError(error_code, message)
                self._sleeper(self._retry_delay(response, attempt))
                continue

            try:
                response.raise_for_status()
            except httpx.HTTPError as exc:
                raise BybitAPIError(f"Bybit request failed: {exc}") from exc
            if payload is None:
                raise BybitAPIError("Bybit returned invalid JSON")
            if code != 0:
                message = payload.get("retMsg", "unknown error")
                raise BybitAPIError(f"Bybit API error {code}: {message}")
            return payload

        raise AssertionError("retry loop completed unexpectedly")

    def _wait_for_request_slot(self) -> None:
        with self._rate_limit_lock:
            now = self._monotonic_clock()
            delay = max(0.0, self._next_request_time - now)
            if delay > 0:
                self._sleeper(delay)
                now = self._monotonic_clock()
            self._next_request_time = max(self._next_request_time, now) + (
                self._request_interval
            )

    def _retry_delay(self, response: httpx.Response, attempt: int) -> float:
        retry_after = _bounded_delay(response.headers.get("Retry-After"))
        if retry_after is not None:
            return retry_after

        reset_timestamp = response.headers.get("X-Bapi-Limit-Reset-Timestamp")
        if reset_timestamp is not None:
            try:
                reset_delay = float(reset_timestamp) / 1000 - self._clock()
            except ValueError:
                pass
            else:
                if 0 <= reset_delay <= MAX_RETRY_DELAY:
                    return reset_delay

        return min(FALLBACK_RETRY_DELAY * pow(2.0, attempt), MAX_RETRY_DELAY)

    def get_usdt_perpetual_symbols(self) -> list[MarketSymbol]:
        payload = self._get(
            "/v5/market/instruments-info",
            {"category": "linear", "status": "Trading", "limit": 1000},
        )
        return [
            MarketSymbol(
                exchange=EXCHANGE,
                symbol=item["symbol"],
                base_asset=item["baseCoin"],
                quote_asset=item["quoteCoin"],
                market_type="perpetual",
                is_active=item["status"] == "Trading",
                symbol_type=item.get("symbolType"),
            )
            for item in payload["result"]["list"]
            if item["quoteCoin"] == "USDT" and item["contractType"] == "LinearPerpetual"
        ]

    def get_usdt_perpetual_tickers(self) -> list[MarketSnapshot]:
        payload = self._get("/v5/market/tickers", {"category": "linear"})
        return [
            MarketSnapshot(
                exchange=EXCHANGE,
                symbol=item["symbol"],
                last_price=Decimal(item["lastPrice"]),
                turnover_24h=Decimal(item["turnover24h"]),
                volume_24h=Decimal(item["volume24h"]),
                index_price=_optional_decimal(item.get("indexPrice")),
                price_change_24h_pct=_optional_decimal(item.get("price24hPcnt")),
                high_price_24h=_optional_decimal(item.get("highPrice24h")),
                low_price_24h=_optional_decimal(item.get("lowPrice24h")),
                open_interest=_optional_decimal(item.get("openInterest")),
                open_interest_value=_optional_decimal(item.get("openInterestValue")),
                funding_rate=_optional_decimal(item.get("fundingRate")),
                next_funding_time=_optional_timestamp(item.get("nextFundingTime")),
            )
            for item in payload["result"]["list"]
            if item["symbol"].endswith("USDT")
        ]

    def get_klines(
        self,
        symbol: str,
        interval: str,
        limit: int = 200,
        *,
        start: datetime | None = None,
        end: datetime | None = None,
    ) -> list[Candle]:
        if not 1 <= limit <= MAX_KLINE_LIMIT:
            raise ValueError(f"limit must be between 1 and {MAX_KLINE_LIMIT}")
        params: dict[str, str | int] = {
            "category": "linear",
            "symbol": symbol,
            "interval": interval,
            "limit": limit,
        }
        if start is not None:
            params["start"] = _timestamp_milliseconds(start)
        if end is not None:
            params["end"] = _timestamp_milliseconds(end)
        payload = self._get(
            "/v5/market/kline",
            params,
        )
        candles = [
            Candle(
                exchange=EXCHANGE,
                symbol=symbol,
                interval=interval,
                open_time=_timestamp(row[0]),
                open=Decimal(row[1]),
                high=Decimal(row[2]),
                low=Decimal(row[3]),
                close=Decimal(row[4]),
                volume=Decimal(row[5]),
                turnover=Decimal(row[6]),
            )
            for row in payload["result"]["list"]
        ]
        return sorted(candles, key=lambda candle: candle.open_time)

    def get_open_interest(
        self, symbol: str, interval: str, limit: int = 50
    ) -> list[OpenInterestPoint]:
        payload = self._get(
            "/v5/market/open-interest",
            {
                "category": "linear",
                "symbol": symbol,
                "intervalTime": interval,
                "limit": limit,
            },
        )
        return _open_interest_points(payload, symbol=symbol, interval=interval)

    def get_historical_open_interest(
        self,
        symbol: str,
        interval: str,
        *,
        start: datetime,
        end: datetime,
        page_limit: int = MAX_OPEN_INTEREST_LIMIT,
        on_page: Callable[[], None] | None = None,
    ) -> list[OpenInterestPoint]:
        """Return a complete bounded historical OI range, oldest first."""
        if interval not in OPEN_INTEREST_INTERVALS:
            supported = ", ".join(sorted(OPEN_INTEREST_INTERVALS))
            raise ValueError(
                f"unsupported open-interest interval {interval!r}; "
                f"choose one of: {supported}"
            )
        if not 1 <= page_limit <= MAX_OPEN_INTEREST_LIMIT:
            raise ValueError(
                f"page_limit must be between 1 and {MAX_OPEN_INTEREST_LIMIT}"
            )
        start_ms = _timestamp_milliseconds(start)
        end_ms = _timestamp_milliseconds(end)
        if end_ms <= start_ms:
            raise ValueError("open-interest end must be later than start")

        points: dict[datetime, OpenInterestPoint] = {}
        cursor: str | None = None
        seen_cursors: set[str] = set()
        while True:
            params: dict[str, str | int] = {
                "category": "linear",
                "symbol": symbol,
                "intervalTime": interval,
                "startTime": start_ms,
                "endTime": end_ms,
                "limit": page_limit,
            }
            if cursor is not None:
                params["cursor"] = cursor
            if on_page is not None:
                on_page()
            payload = self._get("/v5/market/open-interest", params)
            for point in _open_interest_points(
                payload, symbol=symbol, interval=interval
            ):
                timestamp_ms = int(point.timestamp.timestamp() * 1_000)
                if start_ms <= timestamp_ms <= end_ms:
                    points[point.timestamp] = point
            result = payload.get("result")
            next_cursor = (
                result.get("nextPageCursor") if isinstance(result, dict) else None
            )
            if not isinstance(next_cursor, str) or not next_cursor:
                break
            if next_cursor in seen_cursors:
                raise BybitAPIError("Bybit returned a repeated open-interest cursor")
            seen_cursors.add(next_cursor)
            cursor = next_cursor
        return [points[timestamp] for timestamp in sorted(points)]

    def get_funding_rate(self, symbol: str) -> FundingRate:
        payload = self._get(
            "/v5/market/funding/history",
            {"category": "linear", "symbol": symbol, "limit": 1},
        )
        items = payload["result"]["list"]
        if not items:
            raise BybitAPIError(f"No funding rate returned for {symbol}")
        item = items[0]
        return FundingRate(
            exchange=EXCHANGE,
            symbol=symbol,
            funding_rate=Decimal(item["fundingRate"]),
            funding_time=_timestamp(item["fundingRateTimestamp"]),
        )


def _open_interest_points(
    payload: Mapping[str, Any], *, symbol: str, interval: str
) -> list[OpenInterestPoint]:
    result = payload.get("result")
    items = result.get("list") if isinstance(result, dict) else None
    if not isinstance(items, list):
        return []
    points = [
        OpenInterestPoint(
            exchange=EXCHANGE,
            symbol=symbol,
            interval=interval,
            timestamp=_timestamp(item["timestamp"]),
            open_interest=Decimal(item["openInterest"]),
        )
        for item in items
        if isinstance(item, dict)
        and "timestamp" in item
        and "openInterest" in item
    ]
    return sorted(points, key=lambda point: point.timestamp)


def _timestamp(value: str) -> datetime:
    return datetime.fromtimestamp(int(value) / 1000, tz=UTC)


def _optional_decimal(value: object | None) -> Decimal | None:
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    return Decimal(str(value))


def _optional_timestamp(value: object | None) -> datetime | None:
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    return _timestamp(str(value))


def _timestamp_milliseconds(value: datetime) -> int:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("datetime must be timezone-aware")
    return int(value.astimezone(UTC).timestamp() * 1_000)


def _response_payload(response: httpx.Response) -> dict[str, Any] | None:
    try:
        payload: dict[str, Any] = response.json()
    except ValueError:
        return None
    return payload


def _bounded_delay(value: str | None) -> float | None:
    if value is None:
        return None
    try:
        delay = float(value)
    except ValueError:
        return None
    return delay if 0 <= delay <= MAX_RETRY_DELAY else None
