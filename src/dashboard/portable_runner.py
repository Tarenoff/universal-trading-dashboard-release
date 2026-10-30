"""Run a portable Dashboard backed by an explicit SQLite configuration."""

from __future__ import annotations

import argparse
from collections.abc import Sequence
from pathlib import Path

import uvicorn

from dashboard.external_source_config import load_external_sources_configuration
from dashboard.market_data_config import load_market_data_configuration
from dashboard.portable import create_portable_dashboard


def _port(value: str) -> int:
    try:
        port = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("port must be an integer") from exc
    if not 1 <= port <= 65535:
        raise argparse.ArgumentTypeError("port must be between 1 and 65535")
    return port


def main(argv: Sequence[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--events-path",
        type=Path,
        required=True,
        help="Dashboard-owned SQLite EventStore path",
    )
    parser.add_argument(
        "--market-data-config",
        type=Path,
        help="Explicit global market data JSON configuration",
    )
    parser.add_argument(
        "--external-sources-config",
        type=Path,
        required=True,
        help="External SQLite sources JSON configuration",
    )
    parser.add_argument(
        "--history-path",
        type=Path,
        action="append",
        dest="history_paths",
        help="Detailed local history SQLite database; repeat oldest to newest",
    )
    parser.add_argument(
        "--hourly-history-path",
        type=Path,
        help="Optional local hourly history SQLite database",
    )
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=_port, default=8000)
    arguments = parser.parse_args(argv)
    if not arguments.external_sources_config.is_file():
        parser.error(
            "External sources configuration not found: "
            f"{arguments.external_sources_config}"
        )
    if (
        arguments.market_data_config is not None
        and not arguments.market_data_config.is_file()
    ):
        parser.error(
            "Market data configuration not found: " f"{arguments.market_data_config}"
        )
    if arguments.market_data_config is not None and (
        arguments.history_paths or arguments.hourly_history_path is not None
    ):
        parser.error(
            "--market-data-config cannot be combined with history path options"
        )
    try:
        configuration = load_external_sources_configuration(
            arguments.external_sources_config
        )
        market_data_configuration = (
            None
            if arguments.market_data_config is None
            else load_market_data_configuration(arguments.market_data_config)
        )
        app = create_portable_dashboard(
            arguments.events_path,
            configuration,
            detailed_historical_database_paths=arguments.history_paths,
            hourly_history_database_path=arguments.hourly_history_path,
            market_data_configuration=market_data_configuration,
            source_configuration_path=arguments.external_sources_config,
        )
    except (OSError, ValueError) as exc:
        parser.error(f"Unable to start portable dashboard: {exc}")
    uvicorn.run(app, host=arguments.host, port=arguments.port)
