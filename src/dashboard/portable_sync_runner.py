"""Continuously synchronize configured SQLite sources into a portable EventStore."""

from __future__ import annotations

import argparse
import hashlib
import logging
from collections.abc import Sequence
from pathlib import Path

from dashboard.event_store import EventStore
from dashboard.external_source_config import load_external_sources_configuration
from dashboard.external_source_sync import (
    SynchronizerAlreadyRunningError,
    run_synchronizer,
)
from dashboard.portable import portable_signal_type_registry


def _runner_lock_path(events_path: Path, configuration_path: Path) -> Path:
    identity = "\0".join(
        (str(events_path.resolve()), str(configuration_path.resolve()))
    )
    name = hashlib.sha256(identity.encode()).hexdigest() + ".lock"
    return events_path.parent / ".portable-source-sync-locks" / name


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--events-path", type=Path, required=True)
    parser.add_argument("--external-sources-config", type=Path, required=True)
    arguments = parser.parse_args(argv)
    if not arguments.external_sources_config.is_file():
        parser.error(
            "External sources configuration not found: "
            f"{arguments.external_sources_config}"
        )
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s"
    )
    configuration_path = arguments.external_sources_config
    events_path = arguments.events_path
    try:
        run_synchronizer(
            configuration_path,
            EventStore(
                events_path,
                allowed_event_types=lambda: tuple(
                    item.event_type
                    for item in portable_signal_type_registry(
                        load_external_sources_configuration(configuration_path)
                    ).signal_types
                ),
            ),
            allowed_roots=None,
            lock_directory=events_path.parent / ".single-source-import-locks",
            runner_lock_path=_runner_lock_path(events_path, configuration_path),
        )
    except SynchronizerAlreadyRunningError as exc:
        logging.info("%s", exc)
    return 0
