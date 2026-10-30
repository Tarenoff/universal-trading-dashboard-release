"""Exchange-neutral market data models."""

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from enum import StrEnum


class AlertType(StrEnum):
    """Stable identifiers used to route alerts."""

    MARKET_SIGNAL = "market_signal"


class AlertLevel(StrEnum):
    """Importance of a market alert for display prioritization."""

    ACTIVE = "active"
    STRONG = "strong"
    EXTREME = "extreme"


@dataclass(frozen=True, slots=True)
class MarketSymbol:
    exchange: str
    symbol: str
    base_asset: str
    quote_asset: str
    market_type: str
    is_active: bool
    symbol_type: str | None = None


@dataclass(frozen=True, slots=True)
class MarketSnapshot:
    exchange: str
    symbol: str
    last_price: Decimal
    turnover_24h: Decimal
    volume_24h: Decimal
    index_price: Decimal | None = None
    price_change_24h_pct: Decimal | None = None
    high_price_24h: Decimal | None = None
    low_price_24h: Decimal | None = None
    open_interest: Decimal | None = None
    open_interest_value: Decimal | None = None
    funding_rate: Decimal | None = None
    next_funding_time: datetime | None = None


@dataclass(frozen=True, slots=True)
class ActiveMarket:
    exchange: str
    symbol: str
    interval: str
    direction: str
    price_change_pct: Decimal
    volume_multiplier: Decimal
    open_interest_change_pct: Decimal | None
    turnover_24h: Decimal
    current_volume: Decimal
    average_volume: Decimal
    reference_price: Decimal
    source_candle_open_at: datetime
    source_candle_close_at: datetime
    source_candle_closed: bool
    alert_type: AlertType = AlertType.MARKET_SIGNAL
    alert_level: AlertLevel = AlertLevel.ACTIVE


@dataclass(frozen=True, slots=True)
class Candle:
    exchange: str
    symbol: str
    interval: str
    open_time: datetime
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal
    turnover: Decimal


@dataclass(frozen=True, slots=True)
class OpenInterestPoint:
    exchange: str
    symbol: str
    interval: str
    timestamp: datetime
    open_interest: Decimal


@dataclass(frozen=True, slots=True)
class FundingRate:
    exchange: str
    symbol: str
    funding_rate: Decimal
    funding_time: datetime
