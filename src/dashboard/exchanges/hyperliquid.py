"""Client for Hyperliquid's public standard-perpetual candle endpoint."""

from __future__ import annotations

import time
from collections.abc import Callable, Mapping
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import httpx

from dashboard.exchanges.models import Candle

BASE_URL = "https://api.hyperliquid.xyz"
EXCHANGE = "HYPERLIQUID"
INFO_PATH = "/info"
DEFAULT_MAX_ATTEMPTS = 4
FALLBACK_RETRY_DELAY = 1.0
MAX_RETRY_DELAY = 60.0
MAX_CANDLES_PER_RESPONSE = 500
MAX_RETAINED_CANDLES = 5_000
MAX_CANDLE_PAGES = MAX_RETAINED_CANDLES // MAX_CANDLES_PER_RESPONSE
CANDLE_INTERVALS = frozenset(
    {
        "1m",
        "3m",
        "5m",
        "15m",
        "30m",
        "1h",
        "2h",
        "4h",
        "8h",
        "12h",
        "1d",
        "3d",
        "1w",
        "1M",
    }
)
_FIXED_INTERVALS = {
    "1m": timedelta(minutes=1),
    "3m": timedelta(minutes=3),
    "5m": timedelta(minutes=5),
    "15m": timedelta(minutes=15),
    "30m": timedelta(minutes=30),
    "1h": timedelta(hours=1),
    "2h": timedelta(hours=2),
    "4h": timedelta(hours=4),
    "8h": timedelta(hours=8),
    "12h": timedelta(hours=12),
    "1d": timedelta(days=1),
    "3d": timedelta(days=3),
    "1w": timedelta(days=7),
}


class HyperliquidAPIError(RuntimeError):
    """Raised when Hyperliquid returns an invalid or failed API response."""


class HyperliquidRateLimitError(HyperliquidAPIError):
    """Raised when Hyperliquid rate limiting remains after bounded retries."""


class HyperliquidSymbolError(HyperliquidAPIError):
    """Raised when a dashboard symbol cannot safely map to a standard perp."""


class HyperliquidPublicClient:
    """Read standard Hyperliquid perpetual candles without authentication."""

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

    def __enter__(self) -> HyperliquidPublicClient:
        return self

    def __exit__(self, *args: object) -> None:
        self.close()

    def close(self) -> None:
        self._client.close()

    def get_candles(
        self,
        symbol: str,
        interval: str,
        *,
        start: datetime,
        end: datetime,
    ) -> list[Candle]:
        """Return one Hyperliquid candle page, oldest first.

        ``symbol`` is the dashboard's canonical ``BASEUSDT`` perpetual symbol.
        Spot pairs and HIP-3 names are rejected rather than guessed.
        """
        coin = standard_perpetual_coin(symbol)
        _validate_interval(interval)
        start_ms = _timestamp_milliseconds(start)
        end_ms = _timestamp_milliseconds(end)
        if end_ms <= start_ms:
            raise ValueError("candle end must be later than start")
        payload = self._post_info(
            {
                "type": "candleSnapshot",
                "req": {
                    "coin": coin,
                    "interval": interval,
                    "startTime": start_ms,
                    "endTime": end_ms,
                },
            }
        )
        return _candles(payload, symbol=symbol, coin=coin, interval=interval)

    def get_historical_candles(
        self,
        symbol: str,
        interval: str,
        *,
        start: datetime,
        end: datetime,
        max_pages: int = MAX_CANDLE_PAGES,
    ) -> list[Candle]:
        """Return a bounded range within Hyperliquid's 5,000-candle window."""
        _validate_interval(interval)
        start_utc = _as_utc(start)
        end_utc = _as_utc(end)
        if end_utc <= start_utc:
            raise ValueError("candle end must be later than start")
        if not 1 <= max_pages <= MAX_CANDLE_PAGES:
            raise ValueError(
                f"max_pages must be between 1 and {MAX_CANDLE_PAGES} "
                "for Hyperliquid candle retention"
            )

        candles: dict[datetime, Candle] = {}
        cursor = start_utc
        for _ in range(max_pages):
            page = self.get_candles(
                symbol, interval, start=cursor, end=end_utc
            )
            for candle in page:
                if start_utc <= candle.open_time <= end_utc:
                    candles[candle.open_time] = candle
            if len(page) < MAX_CANDLES_PER_RESPONSE:
                break
            next_cursor = _next_candle_cursor(page[-1].open_time, interval)
            if next_cursor <= cursor:
                raise HyperliquidAPIError(
                    "Hyperliquid returned non-advancing candle page"
                )
            cursor = next_cursor
            if cursor > end_utc:
                break
        return [candles[timestamp] for timestamp in sorted(candles)]

    def _post_info(self, request: Mapping[str, object]) -> object:
        for attempt in range(self._max_attempts):
            try:
                response = self._client.post(INFO_PATH, json=request)
            except httpx.HTTPError as exc:
                raise HyperliquidAPIError(
                    f"Hyperliquid request failed: {exc}"
                ) from exc
            if response.status_code == 429:
                if attempt + 1 == self._max_attempts:
                    raise HyperliquidRateLimitError(
                        "Hyperliquid request rate limit exceeded"
                    )
                self._sleeper(_retry_delay(response, attempt))
                continue
            try:
                response.raise_for_status()
            except httpx.HTTPError as exc:
                raise HyperliquidAPIError(_error_message(response)) from exc
            try:
                return response.json()
            except ValueError as exc:
                raise HyperliquidAPIError("Hyperliquid returned invalid JSON") from exc
        raise AssertionError("retry loop completed unexpectedly")


def standard_perpetual_coin(symbol: str) -> str:
    """Map only the dashboard's canonical USDT perpetual symbols to Hyperliquid.

    Hyperliquid spot uses slash or ``@index`` identifiers, while HIP-3 uses
    ``dex:coin``. Neither is accepted here, even if its base asset resembles a
    regular perpetual, because this client has no market-metadata resolver yet.
    """
    if (
        not isinstance(symbol, str)
        or not symbol.endswith("USDT")
        or symbol != symbol.upper()
        or not symbol[:-4]
        or not symbol[:-4].isalnum()
    ):
        raise HyperliquidSymbolError(
            "Hyperliquid standard perpetual symbols must be uppercase BASEUSDT"
        )
    return symbol[:-4]


def _candles(
    payload: object, *, symbol: str, coin: str, interval: str
) -> list[Candle]:
    if not isinstance(payload, list):
        raise HyperliquidAPIError("Hyperliquid returned an invalid candle response")
    candles: dict[datetime, Candle] = {}
    try:
        for item in payload:
            if not isinstance(item, dict):
                raise ValueError("invalid candle item")
            if _required_string(item, "s") != coin:
                raise ValueError("unexpected candle coin")
            if _required_string(item, "i") != interval:
                raise ValueError("unexpected candle interval")
            opened_at = _timestamp(_required_integer(item, "t"))
            open_price = _decimal(item, "o")
            high = _decimal(item, "h")
            low = _decimal(item, "l")
            close = _decimal(item, "c")
            volume = _decimal(item, "v")
            if (
                min(open_price, high, low, close) <= 0
                or volume < 0
                or high < max(open_price, close)
                or low > min(open_price, close)
            ):
                raise ValueError("invalid OHLCV values")
            candles[opened_at] = Candle(
                exchange=EXCHANGE,
                symbol=symbol,
                interval=interval,
                open_time=opened_at,
                open=open_price,
                high=high,
                low=low,
                close=close,
                volume=volume,
                turnover=Decimal("0"),
            )
    except (ArithmeticError, TypeError, ValueError) as exc:
        raise HyperliquidAPIError(
            "Hyperliquid returned an invalid candle response"
        ) from exc
    return [candles[timestamp] for timestamp in sorted(candles)]


def _validate_interval(interval: str) -> None:
    if interval not in CANDLE_INTERVALS:
        raise ValueError(f"unsupported Hyperliquid candle interval: {interval}")


def _next_candle_cursor(open_time: datetime, interval: str) -> datetime:
    if interval != "1M":
        return open_time + _FIXED_INTERVALS[interval]
    if open_time.month == 12:
        return open_time.replace(year=open_time.year + 1, month=1)
    return open_time.replace(month=open_time.month + 1)


def _timestamp(value: int) -> datetime:
    if value < 1_000_000_000_000:
        raise ValueError("candle timestamp must be milliseconds")
    return datetime.fromtimestamp(value / 1_000, tz=UTC)


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


def _decimal(payload: Mapping[str, object], name: str) -> Decimal:
    value = payload.get(name)
    if isinstance(value, bool) or value is None:
        raise ValueError(f"{name} is required")
    decimal = Decimal(str(value))
    if not decimal.is_finite():
        raise ValueError(f"{name} must be finite")
    return decimal


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
        return f"Hyperliquid request failed: HTTP {response.status_code}"
    if isinstance(payload, dict):
        message = payload.get("error") or payload.get("message")
        if isinstance(message, str) and message:
            return f"Hyperliquid request failed: {message}"
    return f"Hyperliquid request failed: HTTP {response.status_code}"
