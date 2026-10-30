"""Explicit portable-dashboard market data selection."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class LocalSqliteMarketData:
    """The one explicit read-only local market source for a dashboard."""

    market_exchange: str
    detailed_history_paths: tuple[Path, ...]
    hourly_history_path: Path | None = None


@dataclass(frozen=True, slots=True)
class BybitApiMarketData:
    """The one explicit public Bybit linear market source for a dashboard."""

    market_exchange: str
    market_category: str


@dataclass(frozen=True, slots=True)
class MarketDataConfiguration:
    """One market-data source selected independently from signal sources."""

    provider: LocalSqliteMarketData | BybitApiMarketData


def load_market_data_configuration(path: Path) -> MarketDataConfiguration:
    """Load explicit local-history selections without opening SQLite databases."""
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"Invalid market data configuration: {exc}") from exc
    if not isinstance(raw, dict):
        raise ValueError("Market data configuration must be a JSON object")
    provider = raw.get("provider")
    if provider == "local_sqlite":
        return MarketDataConfiguration(_local_sqlite_source(raw, path.parent))
    if provider == "bybit_api":
        return MarketDataConfiguration(_bybit_api_source(raw))
    raise ValueError("Unsupported market provider")


def validate_local_market_data(configuration: MarketDataConfiguration) -> None:
    """Validate selected local files before the portable web server starts."""
    source = configuration.provider
    if not isinstance(source, LocalSqliteMarketData):
        return
    for path in source.detailed_history_paths:
        if not path.is_file():
            raise ValueError(f"Historical candle database not found: {path}")
    if (
        source.hourly_history_path is not None
        and not source.hourly_history_path.is_file()
    ):
        raise ValueError(
            "Hourly historical candle database not found: "
            f"{source.hourly_history_path}"
        )


def _local_sqlite_source(raw: dict[str, object], base: Path) -> LocalSqliteMarketData:
    exchange = _string(raw, "market_exchange")
    if exchange != "BYBIT":
        raise ValueError("local_sqlite market_exchange must be BYBIT")
    detailed = raw.get("history_paths")
    if (
        not isinstance(detailed, list)
        or not detailed
        or not all(isinstance(item, str) and item for item in detailed)
    ):
        raise ValueError("history_paths must be a non-empty string list")
    hourly = raw.get("hourly_history_path")
    if hourly is not None and (not isinstance(hourly, str) or not hourly):
        raise ValueError("hourly_history_path must be a string")
    return LocalSqliteMarketData(
        market_exchange=exchange,
        detailed_history_paths=tuple(_path(base, item) for item in detailed),
        hourly_history_path=None if hourly is None else _path(base, hourly),
    )


def _bybit_api_source(raw: dict[str, object]) -> BybitApiMarketData:
    exchange = _string(raw, "market_exchange")
    category = _string(raw, "market_category")
    if exchange != "BYBIT":
        raise ValueError("bybit_api market_exchange must be BYBIT")
    if category != "linear":
        raise ValueError("bybit_api market_category must be linear")
    return BybitApiMarketData(market_exchange=exchange, market_category=category)


def _string(raw: dict[str, object], name: str) -> str:
    value = raw.get(name)
    if not isinstance(value, str) or not value:
        raise ValueError(f"{name} is required")
    return value


def _path(base: Path, value: str) -> Path:
    path = Path(value)
    return path if path.is_absolute() else base / path
