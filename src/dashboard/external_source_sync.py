"""Opt-in foreground scheduler for configured external SQLite sources."""

from __future__ import annotations

import fcntl
import logging
import os
import time
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from pathlib import Path

from dashboard.event_store import EventStore
from dashboard.external_source_config import load_external_sources_configuration
from dashboard.single_source_import import import_single_source

LOGGER = logging.getLogger(__name__)


class SynchronizerAlreadyRunningError(RuntimeError):
    """Raised when another process already owns one scheduler identity."""


@contextmanager
def synchronizer_lock(lock_path: Path) -> Iterator[None]:
    """Hold a non-blocking process lock for one continuous synchronizer."""
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(lock_path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "r+") as lock:
        try:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise SynchronizerAlreadyRunningError(
                "external source synchronizer is already running"
            ) from exc
        try:
            yield
        finally:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


def synchronize_once(
    configuration_path: Path,
    store: EventStore,
    *,
    allowed_roots: Sequence[Path | str] | None,
    lock_directory: Path,
    next_run: dict[str, float] | None = None,
    now: float | None = None,
) -> None:
    """Reload configuration and import eligible sources using the shared lock."""
    configuration = load_external_sources_configuration(configuration_path)
    now = time.monotonic() if now is None else now
    for source in configuration.sources:
        if not source.automatic_sync_enabled:
            if next_run is not None:
                next_run.pop(source.source_id, None)
            continue
        if next_run is not None and now < next_run.get(source.source_id, 0):
            continue
        if next_run is not None:
            next_run[source.source_id] = now + source.sync_interval_seconds
        started_at = time.monotonic()
        result = import_single_source(
            configuration_path,
            source.source_id,
            store,
            allowed_roots=allowed_roots,
            lock_directory=lock_directory,
            confirmed=False,
            preflight_fingerprint="",
            automatic=True,
        )
        elapsed_ms = (time.monotonic() - started_at) * 1_000
        if result.error:
            LOGGER.error(
                "source=%s synchronization failed: %s name=%s elapsed_ms=%.1f",
                source.source_id,
                result.error,
                source.display_name,
                elapsed_ms,
            )
        elif result.rejected:
            LOGGER.warning(
                "source=%s name=%s elapsed_ms=%.1f scanned=%s inserted=%s rejected=%s",
                source.source_id,
                source.display_name,
                elapsed_ms,
                result.scanned,
                result.inserted,
                result.rejected,
            )
        else:
            LOGGER.info(
                "source=%s name=%s elapsed_ms=%.1f scanned=%s inserted=%s rejected=%s",
                source.source_id,
                source.display_name,
                elapsed_ms,
                result.scanned,
                result.inserted,
                result.rejected,
            )


def run_synchronizer(
    configuration_path: Path,
    store: EventStore,
    *,
    allowed_roots: Sequence[Path | str] | None,
    lock_directory: Path,
    sleep: Callable[[float], None] = time.sleep,
    runner_lock_path: Path | None = None,
) -> None:
    """Reload configuration on each cycle and retry after errors."""
    next_run: dict[str, float] = {}
    try:
        if runner_lock_path is None:
            _run_synchronizer_loop(
                configuration_path,
                store,
                allowed_roots=allowed_roots,
                lock_directory=lock_directory,
                next_run=next_run,
                sleep=sleep,
            )
        else:
            with synchronizer_lock(runner_lock_path):
                _run_synchronizer_loop(
                    configuration_path,
                    store,
                    allowed_roots=allowed_roots,
                    lock_directory=lock_directory,
                    next_run=next_run,
                    sleep=sleep,
                )
    except KeyboardInterrupt:
        LOGGER.info("External source synchronizer stopped")


def _run_synchronizer_loop(
    configuration_path: Path,
    store: EventStore,
    *,
    allowed_roots: Sequence[Path | str] | None,
    lock_directory: Path,
    next_run: dict[str, float],
    sleep: Callable[[float], None],
) -> None:
    while True:
        try:
            synchronize_once(
                configuration_path,
                store,
                allowed_roots=allowed_roots,
                lock_directory=lock_directory,
                next_run=next_run,
            )
        except Exception:
            LOGGER.exception("External source synchronization cycle failed")
        sleep(0.5)
