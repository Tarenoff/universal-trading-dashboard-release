"""Shared Settings routes for configured SQLite sources and signal types."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable, Sequence
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from threading import Lock
from typing import cast
from uuid import uuid4

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from starlette.concurrency import run_in_threadpool

from dashboard.event_store import EventStore
from dashboard.external_source_config import (
    create_custom_signal_definition,
    delete_custom_signal_definition,
    load_external_sources_configuration,
    registry_with_display_overrides,
    rename_user_created_source,
    save_external_sources_configuration,
    save_signal_card_layout,
    save_signal_type_display_override,
    set_custom_signal_enabled,
    set_source_visibility,
    set_user_created_source_sync_enabled,
    update_custom_signal_definition,
    update_user_created_source_import_types,
    validate_new_type_display,
)
from dashboard.external_sqlite_import import (
    ExternalSqliteSource,
    UnsupportedMetadataError,
    _metadata_json,
    distinct_external_sqlite_values,
    inspect_external_sqlite_columns,
    inspect_external_sqlite_tables,
    preview_matching_external_sqlite_rows,
)
from dashboard.signal_type_registry import SignalType, SignalTypeRegistry
from dashboard.single_source_import import (
    import_single_source,
    preflight_single_source_import,
    prepare_source_removal,
)
from dashboard.source_metrics import read_source_metrics
from dashboard.source_removal import (
    cancel_source_removal,
    execute_source_removal,
    preflight_source_removal,
    removal_status,
)

PACKAGE_PATH = Path(__file__).parent
templates = Jinja2Templates(directory=PACKAGE_PATH / "templates")


def add_signal_type_settings_routes(
    app: FastAPI,
    *,
    events_path: Path,
    configuration_path: Path | None,
    signal_type_registry: SignalTypeRegistry,
) -> None:
    """Attach the shared Signal Types page and display-save endpoint."""

    @app.get("/settings/signal-types", response_class=HTMLResponse)  # type: ignore[untyped-decorator]
    def signal_types_settings(request: Request) -> HTMLResponse:
        configuration = (
            None
            if configuration_path is None
            else load_external_sources_configuration(configuration_path)
        )
        display_registry = (
            signal_type_registry
            if configuration is None
            else registry_with_display_overrides(configuration)
        )
        card_layout_sources: dict[str, list[dict[str, str]]] = {}
        custom_ids = (
            set()
            if configuration is None
            else {item.signal_id for item in configuration.custom_signals}
        )
        custom_signal_rows = (
            ()
            if configuration is None
            else tuple(
                {
                    "id": item.signal_id,
                    "display_name": item.display_name,
                    "short_label": item.short_label,
                    "source_id": item.source_id,
                    "enabled": item.enabled,
                    "conditions": [
                        {
                            "field": condition.descriptor,
                            "operator": condition.operator,
                            "value": condition.value,
                        }
                        for condition in item.conditions
                    ],
                    "presentation": display_registry.by_event_type[item.signal_id],
                }
                for item in configuration.custom_signals
            )
        )
        if configuration is not None:
            try:
                counts = EventStore(events_path).count_custom_signal_matches(
                    configuration.custom_signals
                )
                for row in custom_signal_rows:
                    row["event_count"] = counts.get(row["id"], 0)
            except FileNotFoundError:
                for row in custom_signal_rows:
                    row["event_count"] = 0
            for source in configuration.sources:
                for type_id in dict.fromkeys(source.type_mapping.values()):
                    card_layout_sources.setdefault(type_id, []).append(
                        {"id": source.source_id, "display_name": source.display_name}
                    )
        return templates.TemplateResponse(
            request,
            "signal_types_settings.html",
            {
                "signal_types": tuple(
                    item
                    for item in display_registry.signal_types
                    if item.event_type not in custom_ids
                ),
                "custom_signals": custom_signal_rows,
                "sources": () if configuration is None else configuration.sources,
                "builtin_ids": frozenset(),
                "editing_available": configuration_path is not None,
                "card_layout_sources": card_layout_sources,
            },
        )

    @app.post("/settings/signal-types/save")  # type: ignore[untyped-decorator]
    async def save_signal_type(request: Request) -> dict[str, str]:
        if configuration_path is None:
            raise HTTPException(status_code=409, detail="Configuration unavailable")
        try:
            payload = await request.json()
            if (
                not isinstance(payload, dict)
                or set(payload) != {"type_id", "display"}
                or not isinstance(payload["type_id"], str)
                or not isinstance(payload["display"], dict)
            ):
                raise ValueError("type_id and display settings are required")
            await run_in_threadpool(
                save_signal_type_display_override,
                configuration_path,
                payload["type_id"],
                payload["display"],
            )
        except (TypeError, ValueError, OSError) as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return {"message": "Saved."}


def add_signal_source_settings_routes(
    app: FastAPI,
    *,
    sources: Sequence[ExternalSqliteSource],
    registered_signal_types: Sequence[SignalType],
    signal_types_provider: Callable[[], Sequence[SignalType]] | None = None,
    allowed_database_roots: Sequence[Path | str] | None,
    configuration_path: Path | None,
    import_store: EventStore | None = None,
    import_lock_directory: Path | None = None,
) -> None:
    """Attach shared source settings and explicit import routes."""
    progress_lock = Lock()
    manual_import_progress: dict[str, tuple[int, bool]] = {}

    @app.get(  # type: ignore[untyped-decorator]
        "/settings/signal-sources", response_class=HTMLResponse
    )
    def signal_sources_settings(request: Request) -> HTMLResponse:
        configured_sources = sources
        if configuration_path is not None:
            configured_sources = load_external_sources_configuration(
                configuration_path
            ).sources
        types_by_id = {item.event_type: item for item in current_types()}
        source_signal_types = {
            source.source_id: [
                types_by_id[type_id]
                for type_id in dict.fromkeys(
                    source.type_mapping[source_type]
                    for source_type in source.allowed_source_types
                    if source_type in source.type_mapping
                )
                if type_id in types_by_id
            ]
            for source in configured_sources
        }
        source_metrics = {}
        if import_store is not None:
            try:
                source_metrics = read_source_metrics(
                    import_store.database_path,
                    [source.source_id for source in configured_sources],
                )
            except (OSError, sqlite3.Error, ValueError):
                # Unavailable metrics are shown as unknown, never as zero.
                pass
        return templates.TemplateResponse(
            request,
            "signal_sources_settings.html",
            {
                "sources": configured_sources,
                "source_signal_types": source_signal_types,
                "source_metrics": source_metrics,
            },
        )

    def current_types() -> Sequence[SignalType]:
        if signal_types_provider is not None:
            return signal_types_provider()
        if configuration_path is None:
            return registered_signal_types
        configured = load_external_sources_configuration(configuration_path)
        existing = {item.event_type: item for item in registered_signal_types}
        existing.update(registry_with_display_overrides(configured).by_event_type)
        return tuple(existing.values())

    def custom_signal_response(definition: object) -> dict[str, object]:
        from dashboard.custom_signals import CustomSignalDefinition

        assert isinstance(definition, CustomSignalDefinition)
        return {
            "id": definition.signal_id,
            "display_name": definition.display_name,
            "short_label": definition.short_label,
            "source_id": definition.source_id,
            "enabled": definition.enabled,
            "conditions": [
                {
                    "field": condition.descriptor,
                    "operator": condition.operator,
                    "value": condition.value,
                }
                for condition in definition.conditions
            ],
        }

    @app.get("/settings/custom-signals")  # type: ignore[untyped-decorator]
    def list_custom_signals() -> dict[str, object]:
        if configuration_path is None:
            raise HTTPException(status_code=409, detail="Configuration unavailable")
        configuration = load_external_sources_configuration(configuration_path)
        return {
            "custom_signals": [
                custom_signal_response(item) for item in configuration.custom_signals
            ]
        }

    @app.get("/settings/custom-signals/fields")  # type: ignore[untyped-decorator]
    def custom_signal_fields(
        source_id: str, field: str | None = None
    ) -> dict[str, object]:
        if configuration_path is None:
            raise HTTPException(status_code=409, detail="Configuration unavailable")
        configuration = load_external_sources_configuration(configuration_path)
        source = next(
            (item for item in configuration.sources if item.source_id == source_id),
            None,
        )
        if source is None:
            raise HTTPException(status_code=404, detail="Source was not found")
        fields: tuple[str, ...] = ()
        if import_store is not None:
            try:
                fields = import_store.list_owned_external_field_descriptors(
                    source.source_id
                )
                if field is not None:
                    if field not in fields:
                        raise ValueError("Field was not found for this source")
                    profile = import_store.profile_owned_external_field(
                        source.source_id, field
                    )
                    return {
                        "source_id": source.source_id,
                        "available_fields": list(fields),
                        "profile": profile,
                    }
            except FileNotFoundError:
                pass
            except ValueError as exc:
                raise HTTPException(status_code=422, detail=str(exc)) from exc
        return {"source_id": source.source_id, "available_fields": list(fields)}

    async def custom_signal_payload(request: Request) -> dict[str, object]:
        payload = await request.json()
        if not isinstance(payload, dict):
            raise ValueError("Custom signal payload must be an object")
        return cast(dict[str, object], payload)

    @app.post("/settings/custom-signals")  # type: ignore[untyped-decorator]
    async def create_custom_signal(request: Request) -> dict[str, object]:
        if configuration_path is None:
            raise HTTPException(status_code=409, detail="Configuration unavailable")
        try:
            definition = await run_in_threadpool(
                create_custom_signal_definition,
                configuration_path,
                await custom_signal_payload(request),
            )
        except (TypeError, ValueError, OSError) as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return custom_signal_response(definition)

    @app.put("/settings/custom-signals/{signal_id}")  # type: ignore[untyped-decorator]
    async def update_custom_signal(
        signal_id: str, request: Request
    ) -> dict[str, object]:
        if configuration_path is None:
            raise HTTPException(status_code=409, detail="Configuration unavailable")
        try:
            definition = await run_in_threadpool(
                update_custom_signal_definition,
                configuration_path,
                signal_id,
                await custom_signal_payload(request),
            )
        except (TypeError, ValueError, OSError) as exc:
            status = 404 if str(exc) == "Custom signal was not found" else 422
            raise HTTPException(status_code=status, detail=str(exc)) from exc
        return custom_signal_response(definition)

    @app.delete("/settings/custom-signals/{signal_id}")  # type: ignore[untyped-decorator]
    async def delete_custom_signal(signal_id: str) -> dict[str, str]:
        if configuration_path is None:
            raise HTTPException(status_code=409, detail="Configuration unavailable")
        try:
            await run_in_threadpool(
                delete_custom_signal_definition, configuration_path, signal_id
            )
        except (TypeError, ValueError, OSError) as exc:
            status = 404 if str(exc) == "Custom signal was not found" else 422
            raise HTTPException(status_code=status, detail=str(exc)) from exc
        return {"message": "Custom signal deleted."}

    @app.post("/settings/custom-signals/{signal_id}/enabled")  # type: ignore[untyped-decorator]
    async def set_custom_signal_state(
        signal_id: str, request: Request
    ) -> dict[str, object]:
        if configuration_path is None:
            raise HTTPException(status_code=409, detail="Configuration unavailable")
        try:
            payload = await request.json()
            if not isinstance(payload, dict) or not isinstance(
                payload.get("enabled"), bool
            ):
                raise ValueError("enabled must be a boolean")
            definition = await run_in_threadpool(
                set_custom_signal_enabled,
                configuration_path,
                signal_id,
                payload["enabled"],
            )
        except (TypeError, ValueError, OSError) as exc:
            status = 404 if str(exc) == "Custom signal was not found" else 422
            raise HTTPException(status_code=status, detail=str(exc)) from exc
        return custom_signal_response(definition)

    @app.get(  # type: ignore[untyped-decorator]
        "/settings/signal-sources/add", response_class=HTMLResponse
    )
    def add_signal_source(request: Request) -> HTMLResponse:
        return templates.TemplateResponse(
            request,
            "signal_source_add.html",
            {"registered_signal_types": current_types()},
        )

    def card_layout_source(source_id: str, type_id: str) -> ExternalSqliteSource:
        if configuration_path is None:
            raise HTTPException(status_code=409, detail="Configuration unavailable")
        try:
            configured = load_external_sources_configuration(configuration_path)
        except (OSError, ValueError) as exc:
            raise HTTPException(
                status_code=422, detail=f"Invalid source configuration: {exc}"
            ) from exc
        source = next(
            (item for item in configured.sources if item.source_id == source_id), None
        )
        if source is None:
            raise HTTPException(status_code=404, detail="Source was not found")
        custom_ids = {
            item.signal_id
            for item in configured.custom_signals
            if item.source_id == source_id
        }
        if type_id not in source.type_mapping.values() and type_id not in custom_ids:
            raise HTTPException(
                status_code=404, detail="Signal type is not mapped by this source"
            )
        return source

    def card_layout_response(
        source: ExternalSqliteSource, type_id: str
    ) -> dict[str, object]:
        path = configuration_path
        assert path is not None
        layout = source.card_layouts.get(type_id)
        available_fields: tuple[str, ...] = ()
        discovery_available = import_store is not None
        if import_store is not None:
            try:
                custom = next(
                    (
                        item
                        for item in load_external_sources_configuration(
                            path
                        ).custom_signals
                        if item.signal_id == type_id
                    ),
                    None,
                )
                if custom is None:
                    available_fields = (
                        import_store.list_external_card_field_descriptors(
                            source.source_id, type_id
                        )
                    )
                    field_samples = import_store.list_external_card_field_samples(
                        source.source_id, type_id
                    )
                    representative_event = import_store.get_external_card_preview_event(
                        source.source_id, type_id
                    )
                else:
                    available_fields, field_samples, representative_event = (
                        import_store.custom_card_context(
                            custom,
                        )
                    )
            except FileNotFoundError:
                discovery_available = False
                field_samples = {}
                representative_event = None
        else:
            field_samples = {}
            representative_event = None
        return {
            "source_id": source.source_id,
            "type_id": type_id,
            "configured": layout is not None,
            "layout": (
                None
                if layout is None
                else {
                    "compact_fields": list(layout.compact_fields),
                    "detail_fields": list(layout.detail_fields),
                }
            ),
            "available_fields": list(available_fields),
            "field_samples": field_samples,
            "representative_event": representative_event,
            "field_discovery_available": discovery_available,
        }

    @app.get(  # type: ignore[untyped-decorator]
        "/settings/signal-types/card-layout-context"
    )
    def card_layout_context(type_id: str) -> dict[str, str]:
        if configuration_path is None:
            raise HTTPException(status_code=409, detail="Configuration unavailable")
        configured = load_external_sources_configuration(configuration_path)
        custom = next(
            (item for item in configured.custom_signals if item.signal_id == type_id),
            None,
        )
        if custom is not None:
            return {"source_id": custom.source_id, "type_id": type_id}
        candidates = [
            source
            for source in configured.sources
            if type_id in source.type_mapping.values()
        ]
        if not candidates:
            raise HTTPException(
                status_code=404, detail="Signal type is not mapped by a source"
            )
        owned_ids: tuple[str, ...] = ()
        if import_store is not None:
            try:
                owned_ids = import_store.list_owned_external_sources_for_event_type(
                    type_id
                )
            except FileNotFoundError:
                pass
        owned_candidates = [
            source for source in candidates if source.source_id in owned_ids
        ]
        if len(owned_candidates) == 1:
            source = owned_candidates[0]
        elif len(candidates) == 1:
            source = candidates[0]
        else:
            raise HTTPException(
                status_code=409,
                detail=(
                    "Card layout source is ambiguous; import owned events from one "
                    "mapped source before editing this type."
                ),
            )
        return {"source_id": source.source_id, "type_id": type_id}

    @app.get("/settings/signal-types/card-layout")  # type: ignore[untyped-decorator]
    def get_card_layout(source_id: str, type_id: str) -> dict[str, object]:
        return card_layout_response(card_layout_source(source_id, type_id), type_id)

    @app.post("/settings/signal-types/card-layout")  # type: ignore[untyped-decorator]
    async def save_card_layout(request: Request) -> dict[str, object]:
        if configuration_path is None:
            raise HTTPException(status_code=409, detail="Configuration unavailable")
        try:
            payload = await request.json()
            if (
                not isinstance(payload, dict)
                or set(payload) != {"source_id", "type_id", "layout"}
                or not isinstance(payload["source_id"], str)
                or not isinstance(payload["type_id"], str)
                or not isinstance(payload["layout"], dict)
            ):
                raise ValueError("source_id, type_id and layout are required")
            source_id = payload["source_id"]
            type_id = payload["type_id"]
            card_layout_source(source_id, type_id)
            await run_in_threadpool(
                save_signal_card_layout,
                configuration_path,
                source_id,
                type_id,
                payload["layout"],
            )
            source = card_layout_source(source_id, type_id)
        except HTTPException:
            raise
        except (TypeError, ValueError, OSError) as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return card_layout_response(source, type_id)

    @app.post("/settings/signal-sources/inspect")  # type: ignore[untyped-decorator]
    async def inspect_source(request: Request) -> dict[str, object]:
        try:
            payload = await request.json()
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="Invalid request") from exc
        if not isinstance(payload, dict) or not isinstance(
            payload.get("database_path"), str
        ):
            raise HTTPException(status_code=422, detail="Database path is required")
        database_path = payload["database_path"]
        try:
            tables = inspect_external_sqlite_tables(
                database_path, allowed_roots=allowed_database_roots
            )
            selected_table = payload.get("table")
            columns = (
                inspect_external_sqlite_columns(
                    database_path,
                    selected_table,
                    allowed_roots=allowed_database_roots,
                )
                if isinstance(selected_table, str) and selected_table
                else []
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {"tables": tables, "columns": columns}

    @app.post("/settings/signal-sources/type-values")  # type: ignore[untyped-decorator]
    async def type_values(request: Request) -> dict[str, object]:
        try:
            payload = await request.json()
            if not isinstance(payload, dict):
                raise ValueError("Invalid request")
            path = _required_string(payload, "database_path")
            table = _required_string(payload, "table")
            column = _required_string(payload, "column")
            columns = inspect_external_sqlite_columns(
                path, table, allowed_roots=allowed_database_roots
            )
            if column not in {item["name"] for item in columns}:
                raise ValueError("Signal type column not found")
            return {
                "values": await run_in_threadpool(
                    distinct_external_sqlite_values,
                    path,
                    table,
                    column,
                    allowed_roots=allowed_database_roots,
                )
            }
        except (TypeError, ValueError) as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @app.post("/settings/signal-sources/preview")  # type: ignore[untyped-decorator]
    async def preview_source(request: Request) -> dict[str, object]:
        try:
            payload = await request.json()
            registered_types = current_types()
            configuration = _normalize_preview_configuration(
                payload, {item.event_type for item in registered_types}
            )
            available_columns = {
                item["name"]
                for item in await run_in_threadpool(
                    inspect_external_sqlite_columns,
                    cast(str, configuration["database_path"]),
                    cast(str, configuration["table"]),
                    allowed_roots=allowed_database_roots,
                )
            }
            mapped_columns = _mapped_columns(configuration["mappings"])
            if not mapped_columns <= available_columns:
                raise ValueError("A selected mapping column no longer exists")
            mappings = cast(dict[str, dict[str, object]], configuration["mappings"])
            signal_mapping = mappings["signal_type"]
            source_column = (
                cast(str, signal_mapping["column"])
                if signal_mapping["kind"] == "column"
                else None
            )
            selected_types = cast(
                dict[str, str], signal_mapping.get("type_mapping", {})
            )
            rows = await run_in_threadpool(
                preview_matching_external_sqlite_rows,
                cast(str, configuration["database_path"]),
                cast(str, configuration["table"]),
                tuple(sorted(available_columns)),
                allowed_roots=allowed_database_roots,
                signal_type_column=source_column,
                selected_types=tuple(selected_types),
            )
        except (TypeError, ValueError) as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        try:
            events, rejected = _normalize_preview_rows(rows, configuration["mappings"])
        except UnsupportedMetadataError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        type_names = {item.event_type: item.display_name for item in registered_types}
        type_names.update(
            {
                item["id"]: item["display_name"]
                for item in cast(list[dict[str, str]], configuration["new_types"])
            }
        )
        for event in events:
            event["signal_type_display_name"] = type_names[
                cast(str, event["signal_type"])
            ]
        return {
            "configuration": {
                "display_name": configuration["display_name"],
                "database_path": configuration["database_path"],
                "table": configuration["table"],
                "mappings": configuration["mappings"],
            },
            "events": events,
            "rejected": rejected,
            "previewed_count": len(rows),
            "type_names": type_names,
        }

    @app.post("/settings/signal-sources/connect")  # type: ignore[untyped-decorator]
    async def connect_source(request: Request) -> dict[str, object]:
        if configuration_path is None:
            raise HTTPException(
                status_code=409,
                detail="No portable source configuration was supplied",
            )
        try:
            configuration = _normalize_preview_configuration(
                await request.json(),
                {item.event_type for item in current_types()},
            )
            _validate_preview_configuration(
                configuration, allowed_database_roots=allowed_database_roots
            )
            source = _persistent_source(configuration, configuration_path)
            save_external_sources_configuration(
                configuration_path,
                source,
                cast(list[dict[str, str]], configuration["new_types"]),
            )
        except (TypeError, ValueError) as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return {
            "source_id": source["id"],
            "message": "Source saved disabled; importing is not enabled.",
        }

    @app.post("/settings/signal-sources/import-preflight")  # type: ignore[untyped-decorator]
    async def import_preflight(request: Request) -> dict[str, object]:
        if (
            configuration_path is None
            or import_store is None
            or import_lock_directory is None
        ):
            raise HTTPException(status_code=409, detail="Import is unavailable")
        try:
            payload = await request.json()
            if not isinstance(payload, dict) or not isinstance(
                payload.get("source_id"), str
            ):
                raise ValueError("source_id is required")
            result = await run_in_threadpool(
                preflight_single_source_import,
                configuration_path,
                payload["source_id"],
                import_store,
                allowed_roots=allowed_database_roots,
            )
        except (TypeError, ValueError, OSError) as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return asdict(result)

    @app.post("/settings/signal-sources/import-once")  # type: ignore[untyped-decorator]
    async def import_once(request: Request) -> dict[str, object]:
        if (
            configuration_path is None
            or import_store is None
            or import_lock_directory is None
        ):
            raise HTTPException(status_code=409, detail="Import is unavailable")
        try:
            payload = await request.json()
            if (
                not isinstance(payload, dict)
                or not isinstance(payload.get("source_id"), str)
                or not isinstance(payload.get("fingerprint"), str)
                or payload.get("confirmed") is not True
            ):
                raise ValueError(
                    "source_id, fingerprint and explicit confirmation are required"
                )
            source_id = payload["source_id"]
            with progress_lock:
                manual_import_progress[source_id] = (0, True)

            def report_progress(processed: int) -> None:
                with progress_lock:
                    manual_import_progress[source_id] = (processed, True)

            try:
                result = await run_in_threadpool(
                    import_single_source,
                    configuration_path,
                    source_id,
                    import_store,
                    allowed_roots=allowed_database_roots,
                    lock_directory=import_lock_directory,
                    confirmed=True,
                    preflight_fingerprint=payload["fingerprint"],
                    progress_callback=report_progress,
                )
            finally:
                with progress_lock:
                    processed, _ = manual_import_progress[source_id]
                    manual_import_progress[source_id] = (processed, False)
        except (TypeError, ValueError) as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        response = asdict(result)
        if result.error is None:
            try:
                metrics = read_source_metrics(
                    import_store.database_path, [payload["source_id"]]
                )
                response["metrics"] = asdict(metrics[payload["source_id"]])
            except (OSError, sqlite3.Error, ValueError):
                # Completion remains known if the read-only metrics refresh fails.
                pass
        return response

    @app.get("/settings/signal-sources/import-progress")  # type: ignore[untyped-decorator]
    def import_progress(source_id: str) -> dict[str, object]:
        """Return in-memory progress for one explicit manual import."""
        with progress_lock:
            processed, running = manual_import_progress.get(source_id, (0, False))
        return {"processed": processed, "running": running}

    @app.post("/settings/signal-sources/removal-preflight")  # type: ignore[untyped-decorator]
    async def removal_preflight(request: Request) -> dict[str, object]:
        if configuration_path is None or import_store is None:
            raise HTTPException(status_code=409, detail="Removal is unavailable")
        try:
            payload = await request.json()
            if not isinstance(payload, dict) or not isinstance(
                payload.get("source_id"), str
            ):
                raise ValueError("source_id is required")
            plan = await run_in_threadpool(
                preflight_source_removal,
                configuration_path,
                payload["source_id"],
                import_store,
            )
            return asdict(plan)
        except (ValueError, OSError, sqlite3.Error) as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @app.post("/settings/signal-sources/removal-status")  # type: ignore[untyped-decorator]
    async def get_removal_status(request: Request) -> dict[str, object]:
        if configuration_path is None or import_store is None:
            raise HTTPException(status_code=409, detail="Removal is unavailable")
        try:
            payload = await request.json()
            if not isinstance(payload, dict) or not isinstance(
                payload.get("source_id"), str
            ):
                raise ValueError("source_id is required")
            return cast(
                dict[str, object],
                await run_in_threadpool(
                    removal_status,
                    configuration_path,
                    payload["source_id"],
                    import_store,
                ),
            )
        except (ValueError, OSError, sqlite3.Error) as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @app.post("/settings/signal-sources/remove")  # type: ignore[untyped-decorator]
    async def remove_source(request: Request) -> dict[str, str]:
        if (
            configuration_path is None
            or import_store is None
            or import_lock_directory is None
        ):
            raise HTTPException(status_code=409, detail="Removal is unavailable")
        try:
            payload = await request.json()
            if (
                not isinstance(payload, dict)
                or not isinstance(payload.get("source_id"), str)
                or not isinstance(payload.get("fingerprint"), str)
                or payload.get("confirmed") is not True
            ):
                raise ValueError(
                    "source_id, fingerprint and explicit confirmation are required"
                )
            status = await run_in_threadpool(
                execute_source_removal,
                configuration_path,
                payload["source_id"],
                import_store,
                import_lock_directory,
                confirmed=True,
                fingerprint=payload["fingerprint"],
            )
            return {"status": status}
        except (ValueError, OSError, sqlite3.Error) as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    async def change_removal_preparation(
        request: Request, pending: bool
    ) -> dict[str, str]:
        if (
            configuration_path is None
            or import_lock_directory is None
            or import_store is None
        ):
            raise HTTPException(
                status_code=409, detail="Removal preparation unavailable"
            )
        try:
            payload = await request.json()
            if (
                not isinstance(payload, dict)
                or not isinstance(payload.get("source_id"), str)
                or payload.get("confirmed") is not True
            ):
                raise ValueError("source_id and explicit confirmation are required")
            if pending:
                await run_in_threadpool(
                    prepare_source_removal,
                    configuration_path,
                    payload["source_id"],
                    import_lock_directory,
                    confirmed=True,
                    pending=True,
                )
            else:
                await run_in_threadpool(
                    cancel_source_removal,
                    configuration_path,
                    payload["source_id"],
                    import_store,
                    import_lock_directory,
                    confirmed=True,
                )
        except (TypeError, ValueError, OSError, sqlite3.Error) as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return {
            "message": "Removal preparation started."
            if pending
            else "Removal preparation cancelled."
        }

    @app.post("/settings/signal-sources/removal-prepare")  # type: ignore[untyped-decorator]
    async def begin_removal(request: Request) -> dict[str, str]:
        return await change_removal_preparation(request, True)

    @app.post("/settings/signal-sources/removal-cancel")  # type: ignore[untyped-decorator]
    async def cancel_removal(request: Request) -> dict[str, str]:
        return await change_removal_preparation(request, False)

    @app.post("/settings/signal-sources/enabled")  # type: ignore[untyped-decorator]
    async def set_source_enabled(request: Request) -> dict[str, str]:
        if configuration_path is None:
            raise HTTPException(
                status_code=409,
                detail="No portable source configuration was supplied",
            )
        try:
            payload = await request.json()
            if (
                not isinstance(payload, dict)
                or not isinstance(payload.get("source_id"), str)
                or not isinstance(payload.get("enabled"), bool)
            ):
                raise ValueError("source_id and enabled are required")
            set_source_visibility(
                configuration_path, payload["source_id"], payload["enabled"]
            )
        except (TypeError, ValueError) as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return {"message": "Source visibility saved."}

    @app.post("/settings/signal-sources/auto-sync")  # type: ignore[untyped-decorator]
    async def set_source_auto_sync(request: Request) -> dict[str, str]:
        if configuration_path is None:
            raise HTTPException(status_code=409, detail="Configuration unavailable")
        try:
            payload = await request.json()
            if (
                not isinstance(payload, dict)
                or not isinstance(payload.get("source_id"), str)
                or not isinstance(payload.get("enabled"), bool)
            ):
                raise ValueError("source_id and enabled are required")
            set_user_created_source_sync_enabled(
                configuration_path, payload["source_id"], payload["enabled"]
            )
        except (TypeError, ValueError, OSError) as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return {
            "message": (
                "Auto Sync setting saved. Synchronization requires the scheduler "
                "to be running."
            )
        }

    @app.post("/settings/signal-sources/edit")  # type: ignore[untyped-decorator]
    async def edit_source(request: Request) -> dict[str, str]:
        if configuration_path is None:
            raise HTTPException(
                status_code=409,
                detail="No portable source configuration was supplied",
            )
        try:
            payload = await request.json()
            if (
                not isinstance(payload, dict)
                or not isinstance(payload.get("source_id"), str)
                or not isinstance(payload.get("display_name"), str)
            ):
                raise ValueError("source_id and display_name are required")
            rename_user_created_source(
                configuration_path, payload["source_id"], payload["display_name"]
            )
        except (TypeError, ValueError) as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return {"message": "Source display name saved."}

    @app.get("/settings/signal-sources/{source_id}/import-types")  # type: ignore[untyped-decorator]
    async def source_import_types(source_id: str) -> dict[str, object]:
        if configuration_path is None:
            raise HTTPException(status_code=409, detail="Configuration unavailable")
        source = next(
            (
                item
                for item in load_external_sources_configuration(
                    configuration_path
                ).sources
                if item.source_id == source_id
            ),
            None,
        )
        if source is None or not source_id.startswith("sqlite-"):
            raise HTTPException(status_code=404, detail="Source was not found")
        if source.signal_type_column is None:
            return {"fixed_type": True}
        try:
            values = await run_in_threadpool(
                distinct_external_sqlite_values,
                source.database_path,
                source.table_name,
                source.signal_type_column,
                allowed_roots=allowed_database_roots,
            )
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return {
            "fixed_type": False,
            "values": values,
            "selected_types": sorted(source.allowed_source_types),
            "type_mapping": dict(source.type_mapping),
            "type_names": {
                item.event_type: item.display_name for item in current_types()
            },
        }

    @app.post("/settings/signal-sources/edit-import-types")  # type: ignore[untyped-decorator]
    async def edit_source_import_types(request: Request) -> dict[str, str]:
        if configuration_path is None:
            raise HTTPException(status_code=409, detail="Configuration unavailable")
        try:
            payload = await request.json()
            if not isinstance(payload, dict) or not isinstance(
                payload.get("source_id"), str
            ) or not isinstance(payload.get("display_name"), str
            ) or not isinstance(payload.get("selected_types"), list) or not isinstance(
                payload.get("type_mapping"), dict
            ) or not isinstance(payload.get("new_types", []), list
            ):
                raise ValueError(
                    "source_id, display_name, selected_types and type_mapping "
                    "are required"
                )
            selected_types = payload["selected_types"]
            type_mapping = payload["type_mapping"]
            new_types = payload.get("new_types", [])
            if not all(isinstance(item, str) for item in selected_types) or not all(
                isinstance(key, str) and isinstance(value, str)
                for key, value in type_mapping.items()
            ):
                raise ValueError("Signal types and mappings must be strings")
            source = next(
                item
                for item in load_external_sources_configuration(
                    configuration_path
                ).sources
                if item.source_id == payload["source_id"]
            )
            if source.signal_type_column is None:
                raise ValueError("Fixed-type sources have no selectable signal types")
            available = set(
                await run_in_threadpool(
                    distinct_external_sqlite_values,
                    source.database_path,
                    source.table_name,
                    source.signal_type_column,
                    allowed_roots=allowed_database_roots,
                )
            )
            if not set(selected_types) <= available:
                raise ValueError(
                    "Selected signal type is not available from this source"
                )
            await run_in_threadpool(
                update_user_created_source_import_types,
                configuration_path,
                payload["source_id"],
                selected_types,
                type_mapping,
                payload["display_name"],
                new_types,
            )
        except (StopIteration, TypeError, ValueError, OSError) as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return {
            "message": "Import signal types saved. Existing events were not changed."
        }


def _normalize_preview_configuration(
    payload: object, registered_event_types: set[str]
) -> dict[str, object]:
    if not isinstance(payload, dict):
        raise ValueError("Preview request must be an object")
    new_types = payload.get("new_types", [])
    if not isinstance(new_types, list):
        raise ValueError("new_types must be a list")
    staged = []
    for item in new_types:
        if not isinstance(item, dict) or set(item) != {
            "id",
            "display_name",
            "short_label",
        }:
            raise ValueError("Invalid staged signal type")
        identifier = _required_string(item, "id")
        try:
            from uuid import UUID

            if (
                not identifier.startswith("custom_")
                or UUID(identifier[7:]).hex != identifier[7:]
            ):
                raise ValueError
        except (ValueError, AttributeError) as exc:
            raise ValueError("Invalid staged signal type ID") from exc
        name = _required_string(item, "display_name")
        label = item.get("short_label")
        if not isinstance(label, str):
            raise ValueError("Short label is required")
        name, label = validate_new_type_display(name, label)
        if identifier in registered_event_types:
            raise ValueError("Duplicate staged signal type")
        registered_event_types.add(identifier)
        staged.append(
            {
                "id": identifier,
                "display_name": name,
                "abbreviation": label,
                "marker_color": "#808080",
                "marker_shape": "circle",
            }
        )
    source = payload.get("source")
    mappings = payload.get("mappings")
    if not isinstance(source, dict) or not isinstance(mappings, dict):
        raise ValueError("Preview source and mappings are required")
    display_name = _required_string(source, "display_name")
    database_path = _required_string(source, "database_path")
    table_name = _required_string(source, "table")
    normalized: dict[str, dict[str, object]] = {}
    optional_columns = {"price", "timeframe", "signal_level", "direction"}
    for name in (
        "event_id",
        "symbol",
        "event_at",
        "price",
        "timeframe",
        "signal_level",
        "direction",
    ):
        value = mappings.get(name)
        if value is None and name in optional_columns:
            continue
        normalized[name] = dict(_column_mapping(value, name))
    normalized["signal_type"] = dict(
        _value_mapping(
            mappings.get("signal_type"), "signal_type", registered_event_types
        )
    )
    if normalized["signal_type"]["kind"] == "column":
        raw_signal_type = mappings["signal_type"]
        assert isinstance(raw_signal_type, dict)
        pairs = raw_signal_type.get("type_mapping")
        if not isinstance(pairs, list) or not pairs:
            raise ValueError("At least one signal type mapping is required")
        type_mapping: dict[str, str] = {}
        for pair in pairs:
            if not isinstance(pair, dict):
                raise ValueError("Invalid signal type mapping")
            source_value, target = pair.get("source"), pair.get("target")
            if not isinstance(source_value, str) or not source_value.strip():
                raise ValueError("Signal type source value must not be empty")
            if source_value in type_mapping:
                raise ValueError("Signal type source values must be unique")
            if not isinstance(target, str) or target not in registered_event_types:
                raise ValueError("Signal type destination must be registered")
            type_mapping[source_value] = target
        normalized["signal_type"]["type_mapping"] = type_mapping
    used_types = set(
        cast(dict[str, str], normalized["signal_type"].get("type_mapping", {})).values()
    )
    if normalized["signal_type"].get("kind") == "fixed":
        used_types.add(cast(str, normalized["signal_type"]["value"]))
    if any(item["id"] not in used_types for item in staged):
        raise ValueError("Every staged type must be used by the source")
    exchange = mappings.get("exchange")
    if exchange is not None:
        normalized["exchange"] = dict(
            _value_mapping(exchange, "exchange", {"BYBIT", "BINANCE_FUTURES"})
        )
    metadata = mappings.get("metadata_columns", [])
    if not isinstance(metadata, list) or not all(
        isinstance(item, str) and item for item in metadata
    ):
        raise ValueError("metadata_columns must be a string list")
    if len(metadata) != len(set(metadata)):
        raise ValueError("metadata_columns must not contain duplicates")
    for name in metadata:
        normalized[f"metadata:{name}"] = {"kind": "column", "column": name}
    return {
        "new_types": staged,
        "display_name": display_name,
        "database_path": database_path,
        "table": table_name,
        "mappings": normalized,
    }


def _required_string(value: dict[str, object], name: str) -> str:
    item = value.get(name)
    if not isinstance(item, str) or not item.strip():
        raise ValueError(f"{name} is required")
    return item.strip()


def _column_mapping(value: object, name: str) -> dict[str, str]:
    if not isinstance(value, dict) or value.get("kind") != "column":
        raise ValueError(f"{name} must map to a source column")
    column = value.get("column")
    if not isinstance(column, str) or not column:
        raise ValueError(f"{name} column is required")
    return {"kind": "column", "column": column}


def _value_mapping(value: object, name: str, fixed_values: set[str]) -> dict[str, str]:
    if not isinstance(value, dict):
        raise ValueError(f"{name} mapping is required")
    kind = value.get("kind")
    if kind == "column":
        return _column_mapping(value, name)
    fixed = value.get("value")
    if kind != "fixed" or not isinstance(fixed, str) or fixed not in fixed_values:
        raise ValueError(f"Invalid fixed {name}")
    return {"kind": "fixed", "value": fixed}


def _mapped_columns(mappings: object) -> set[str]:
    assert isinstance(mappings, dict)
    return {
        value["column"]
        for value in mappings.values()
        if isinstance(value, dict) and value.get("kind") == "column"
    }


def _validate_preview_configuration(
    configuration: dict[str, object],
    *,
    allowed_database_roots: Sequence[Path | str] | None = None,
) -> None:
    columns = {
        item["name"]
        for item in inspect_external_sqlite_columns(
            cast(str, configuration["database_path"]),
            cast(str, configuration["table"]),
            allowed_roots=allowed_database_roots,
        )
    }
    if not _mapped_columns(configuration["mappings"]) <= columns:
        raise ValueError("A selected mapping column no longer exists")


def _persistent_source(
    configuration: dict[str, object], configuration_path: Path
) -> dict[str, object]:
    mappings = configuration["mappings"]
    assert isinstance(mappings, dict)
    signal_type = mappings["signal_type"]
    assert isinstance(signal_type, dict)
    exchange = mappings.get("exchange", {"kind": "fixed", "value": ""})
    assert isinstance(exchange, dict)

    def column(name: str) -> str:
        value = mappings[name]
        assert isinstance(value, dict)
        return str(value["column"])

    database_path = Path(str(configuration["database_path"])).resolve()
    source: dict[str, object] = {
        "id": f"sqlite-{uuid4()}",
        "source_origin": "universal_wizard_v1",
        "display_name": configuration["display_name"],
        "database_path": str(database_path),
        "table": configuration["table"],
        "primary_key_column": column("event_id"),
        "symbol_column": column("symbol"),
        "timestamp_column": column("event_at"),
        "exchange": exchange.get("value", ""),
        "enabled": False,
        "visibility_enabled": False,
        "sync_enabled": False,
        "field_mappings": mappings,
        "retain_all_columns": True,
    }
    if exchange.get("kind") == "column":
        source["exchange_column"] = exchange["column"]
    if signal_type["kind"] == "fixed":
        event_type = str(signal_type["value"])
        source.update(
            fixed_signal_type=event_type,
            selected_types=[event_type],
            type_mapping={event_type: event_type},
        )
    else:
        type_mapping = signal_type["type_mapping"]
        assert isinstance(type_mapping, dict)
        source.update(
            signal_type_column=signal_type["column"],
            selected_types=list(type_mapping),
            type_mapping=type_mapping,
        )
    price = mappings.get("price")
    if isinstance(price, dict) and price.get("kind") == "column":
        source["price_column"] = price["column"]
    return source


def _normalize_preview_rows(
    rows: list[dict[str, object]], mappings: object
) -> tuple[list[dict[str, object]], list[dict[str, object]]]:
    assert isinstance(mappings, dict)
    events: list[dict[str, object]] = []
    rejected: list[dict[str, object]] = []
    for row_number, row in enumerate(rows, start=1):
        try:
            signal_mapping = mappings["signal_type"]
            source_type = _mapped_required(row, mappings, "signal_type")
            type_mapping = signal_mapping.get("type_mapping")
            unmapped = (
                isinstance(type_mapping, dict) and source_type not in type_mapping
            )
            if unmapped:
                continue
            resolved_type = (
                type_mapping.get(source_type)
                if isinstance(type_mapping, dict)
                else source_type
            )
            event = {
                "event_id": _mapped_required(row, mappings, "event_id"),
                "symbol": _mapped_required(row, mappings, "symbol"),
                "event_at": _normalized_timestamp(
                    _mapped_required(row, mappings, "event_at")
                ),
                "signal_type": resolved_type,
                "signal_type_unmapped": source_type if unmapped else None,
                "exchange": _mapped_value(row, mappings.get("exchange")) or "—",
                "price": _mapped_value(row, mappings.get("price")) or "—",
                "optional": {
                    name: _mapped_value(row, mappings.get(name))
                    for name in ("timeframe", "signal_level", "direction")
                    if name in mappings
                },
                "metadata": json.loads(
                    _metadata_json(row, {name: name for name in row}, strict=True)
                ),
            }
            events.append(event)
        except ValueError as exc:
            rejected.append({"row": row_number, "reason": str(exc)})
    return events, rejected


def _mapped_required(
    row: dict[str, object], mappings: dict[str, object], name: str
) -> str:
    value = _mapped_value(row, mappings[name])
    if value is None or not value.strip():
        raise ValueError(f"Missing {name.replace('_', ' ')}")
    return value


def _mapped_value(row: dict[str, object], mapping: object) -> str | None:
    if mapping is None:
        return None
    assert isinstance(mapping, dict)
    if mapping["kind"] == "fixed":
        return cast(str, mapping["value"])
    value = row.get(mapping["column"])
    return None if value is None else str(value)


def _normalized_timestamp(value: str) -> str:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("Invalid event timestamp") from exc
    if parsed.tzinfo is None:
        raise ValueError("Event timestamp must include a timezone")
    return parsed.astimezone(UTC).isoformat()
