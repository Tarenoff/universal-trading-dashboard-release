"""JSON configuration loader for external SQLite dashboard sources."""

from __future__ import annotations

import fcntl
import inspect
import json
import os
import re
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from functools import wraps
from pathlib import Path
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from dashboard.custom_signals import CustomSignalDefinition, parse_custom_signals
from dashboard.external_sqlite_import import (
    CARD_LAYOUT_STANDARD_FIELDS,
    ExternalSqliteSource,
    SignalCardLayout,
)
from dashboard.signal_type_registry import (
    PRODUCTION_SIGNAL_TYPE_REGISTRY,
    SignalType,
    SignalTypeRegistry,
)

_SHAPES = frozenset(
    {
        "circle",
        "arrowUp",
        "arrowDown",
        "square",
        "diamond",
        "triangleUp",
        "triangleDown",
        "star",
        "target",
        "flag",
    }
)
_POSITIONS = frozenset({"aboveBar", "belowBar", "inBar"})
_DISPLAY_FIELDS = frozenset(
    {"display_name", "short_label", "color", "marker_shape", "marker_position"}
)


def _locked_configuration_write[**P, R](fn: Callable[P, R]) -> Callable[P, R]:
    """Serialize every cooperating writer across the entire read/modify/write."""

    @wraps(fn)
    def wrapped(*args: P.args, **kwargs: P.kwargs) -> R:
        bound = inspect.signature(fn).bind(*args, **kwargs)
        path = Path(next(iter(bound.arguments.values()))).resolve()
        path.parent.mkdir(parents=True, exist_ok=True)
        lock_path = path.with_name(path.name + ".lock")
        fd = os.open(lock_path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "r+") as lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            return fn(*args, **kwargs)

    return wrapped


@dataclass(frozen=True, slots=True)
class ExternalSourcesConfiguration:
    sources: tuple[ExternalSqliteSource, ...]
    signal_types: SignalTypeRegistry
    signal_type_display_overrides: dict[str, dict[str, str]] = field(
        default_factory=dict
    )
    custom_signals: tuple[CustomSignalDefinition, ...] = ()


@_locked_configuration_write
def initialize_external_sources_runtime_configuration(
    runtime_path: Path, example_path: Path
) -> None:
    """Create a validated runtime config from an example without overwriting it."""
    if runtime_path.exists():
        if not runtime_path.is_file():
            raise ValueError("Runtime external source configuration is not a file")
        return
    try:
        contents = example_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ValueError(f"Could not read external source example: {exc}") from exc
    temporary = runtime_path.with_suffix(runtime_path.suffix + ".tmp")
    try:
        runtime_path.parent.mkdir(parents=True, exist_ok=True)
        temporary.write_text(contents, encoding="utf-8")
        load_external_sources_configuration(temporary)
        try:
            os.link(temporary, runtime_path)
        except FileExistsError:
            return
    except OSError as exc:
        raise ValueError(
            f"Could not initialize runtime external source configuration: {exc}"
        ) from exc
    finally:
        temporary.unlink(missing_ok=True)


def load_external_sources_configuration(path: Path) -> ExternalSourcesConfiguration:
    """Load and validate JSON only; this function never imports a source."""
    path = Path(path).resolve()
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"Invalid external source configuration: {exc}") from exc
    if not isinstance(raw, dict):
        raise ValueError("Configuration must be a JSON object")
    types = tuple(_signal_type(item) for item in _list(raw, "signal_types"))
    registry = SignalTypeRegistry(types)
    sources = tuple(_source(item, path.parent) for item in _list(raw, "sources"))
    if len({item.source_id for item in sources}) != len(sources):
        raise ValueError("Source IDs must be unique")
    known = set(PRODUCTION_SIGNAL_TYPE_REGISTRY.by_event_type) | set(
        registry.by_event_type
    )
    for source in sources:
        unknown = set(source.type_mapping.values()) - known
        if unknown:
            raise ValueError(f"Unknown dashboard type mapping: {sorted(unknown)[0]}")
    custom_signals = parse_custom_signals(
        raw.get("custom_signals"),
        source_ids={item.source_id for item in sources},
        existing_type_ids=set(registry.by_event_type),
    )
    for source in sources:
        allowed_layouts = set(source.type_mapping.values()) | {
            item.signal_id
            for item in custom_signals
            if item.source_id == source.source_id
        }
        if set(source.card_layouts) - allowed_layouts:
            raise ValueError("Card layout signal type must be mapped by its source")
    overrides = _validated_display_overrides(
        raw.get("signal_type_display_overrides", {}),
        _registry_with_custom_signals(registry, custom_signals),
    )
    return ExternalSourcesConfiguration(sources, registry, overrides, custom_signals)


@_locked_configuration_write
def save_external_sources_configuration(
    path: Path, source: dict[str, object], new_types: list[dict[str, str]] | None = None
) -> None:
    """Atomically append a validated, disabled source to a portable JSON config."""
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"Invalid external source configuration: {exc}") from exc
    if not isinstance(raw, dict) or not isinstance(raw.get("sources"), list):
        raise ValueError("Configuration must contain a sources list")
    raw["sources"].append(source)
    types = raw.get("signal_types")
    if not isinstance(types, list):
        raise ValueError("Configuration must contain a signal_types list")
    types.extend(new_types or [])
    temporary = path.with_suffix(path.suffix + ".tmp")
    try:
        temporary.write_text(json.dumps(raw, indent=2) + "\n", encoding="utf-8")
        load_external_sources_configuration(temporary)
        os.replace(temporary, path)
    except OSError as exc:
        raise ValueError(
            f"Could not save external source configuration: {exc}"
        ) from exc
    finally:
        temporary.unlink(missing_ok=True)


@_locked_configuration_write
def create_custom_signal_definition(
    path: Path, definition: dict[str, object]
) -> CustomSignalDefinition:
    """Append one validated custom definition without changing source data."""
    _validate_custom_signal_payload(definition)
    raw = _read_configuration_object(path)
    definitions = raw.setdefault("custom_signals", [])
    if not isinstance(definitions, list):
        raise ValueError("custom_signals must be a list")
    signal_id = f"custom_{uuid4().hex}"
    definitions.append({"id": signal_id, "enabled": False, **definition})
    saved = _save_configuration_object(path, raw)
    return _custom_signal_by_id(saved, signal_id)


@_locked_configuration_write
def update_custom_signal_definition(
    path: Path, signal_id: str, definition: dict[str, object]
) -> CustomSignalDefinition:
    """Replace one definition while preserving its stable identity and order."""
    _validate_custom_signal_payload(definition)
    raw = _read_configuration_object(path)
    definitions = raw.get("custom_signals")
    if not isinstance(definitions, list):
        raise ValueError("Custom signal was not found")
    for index, item in enumerate(definitions):
        if isinstance(item, dict) and item.get("id") == signal_id:
            definitions[index] = {
                "id": signal_id,
                "enabled": item.get("enabled", True),
                **definition,
            }
            saved = _save_configuration_object(path, raw)
            return _custom_signal_by_id(saved, signal_id)
    raise ValueError("Custom signal was not found")


@_locked_configuration_write
def delete_custom_signal_definition(path: Path, signal_id: str) -> None:
    """Remove one definition and only its custom-specific saved settings."""
    raw = _read_configuration_object(path)
    definitions = raw.get("custom_signals")
    if not isinstance(definitions, list):
        raise ValueError("Custom signal was not found")
    definition = next(
        (
            item
            for item in definitions
            if isinstance(item, dict) and item.get("id") == signal_id
        ),
        None,
    )
    if definition is None:
        raise ValueError("Custom signal was not found")
    retained = [
        item
        for item in definitions
        if not isinstance(item, dict) or item.get("id") != signal_id
    ]
    raw["custom_signals"] = retained
    overrides = raw.get("signal_type_display_overrides")
    if isinstance(overrides, dict):
        overrides.pop(signal_id, None)
    source_id = definition.get("source_id")
    sources = raw.get("sources")
    if isinstance(source_id, str) and isinstance(sources, list):
        for source in sources:
            if not isinstance(source, dict) or source.get("id") != source_id:
                continue
            layouts = source.get("card_layouts")
            if isinstance(layouts, dict):
                layouts.pop(signal_id, None)
            break
    _save_configuration_object(path, raw)


@_locked_configuration_write
def set_custom_signal_enabled(
    path: Path, signal_id: str, enabled: bool
) -> CustomSignalDefinition:
    if not isinstance(enabled, bool):
        raise ValueError("Custom signal enabled must be a boolean")
    raw = _read_configuration_object(path)
    definitions = raw.get("custom_signals")
    if not isinstance(definitions, list):
        raise ValueError("Custom signal was not found")
    for item in definitions:
        if isinstance(item, dict) and item.get("id") == signal_id:
            item["enabled"] = enabled
            saved = _save_configuration_object(path, raw)
            return _custom_signal_by_id(saved, signal_id)
    raise ValueError("Custom signal was not found")


def _validate_custom_signal_payload(definition: dict[str, object]) -> None:
    required = {"source_id", "display_name", "short_label", "conditions"}
    if set(definition) != required:
        raise ValueError(
            "Custom signal requires source_id, display_name, short_label and conditions"
        )


def _read_configuration_object(path: Path) -> dict[str, object]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"Invalid external source configuration: {exc}") from exc
    if not isinstance(raw, dict):
        raise ValueError("Configuration must be a JSON object")
    return raw


def _save_configuration_object(
    path: Path, raw: dict[str, object]
) -> ExternalSourcesConfiguration:
    temporary = path.with_suffix(path.suffix + ".tmp")
    try:
        temporary.write_text(json.dumps(raw, indent=2) + "\n", encoding="utf-8")
        saved = load_external_sources_configuration(temporary)
        os.replace(temporary, path)
        return saved
    except OSError as exc:
        raise ValueError(
            f"Could not save external source configuration: {exc}"
        ) from exc
    finally:
        temporary.unlink(missing_ok=True)


def _custom_signal_by_id(
    configuration: ExternalSourcesConfiguration, signal_id: str
) -> CustomSignalDefinition:
    result = next(
        (item for item in configuration.custom_signals if item.signal_id == signal_id),
        None,
    )
    if result is None:
        raise RuntimeError("Saved custom signal was not found")
    return result


@_locked_configuration_write
def set_preparatory_removal(path: Path, source_id: str, pending: bool) -> None:
    """Change only the reversible preparation flag, under the JSON writer lock."""
    if not pending:
        raise ValueError("Cancellation requires the EventStore completion check")
    raw = json.loads(path.read_text(encoding="utf-8"))
    sources = raw.get("sources") if isinstance(raw, dict) else None
    if not isinstance(sources, list):
        raise ValueError("Configuration must contain a sources list")
    matches = [s for s in sources if isinstance(s, dict) and s.get("id") == source_id]
    if len(matches) != 1 or matches[0].get("source_origin") != "universal_wizard_v1":
        raise ValueError("Only universal wizard sources are eligible")
    if any(
        isinstance(item, dict) and item.get("source_id") == source_id
        for item in raw.get("custom_signals", [])
    ):
        raise ValueError("Source is referenced by a custom signal")
    source = matches[0]
    if source.get("pending_removal", False) is pending:
        message = "Removal is already pending" if pending else "Removal is not pending"
        raise ValueError(message)
    if pending:
        source["pending_removal"] = True
    else:
        source.pop("pending_removal", None)
    temporary = path.with_suffix(path.suffix + ".tmp")
    try:
        with temporary.open("w", encoding="utf-8") as output:
            output.write(json.dumps(raw, indent=2) + "\n")
            output.flush()
            os.fsync(output.fileno())
        load_external_sources_configuration(temporary)
        os.replace(temporary, path)
        directory_fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        temporary.unlink(missing_ok=True)


@_locked_configuration_write
def remove_user_created_source(path: Path, source_id: str) -> None:
    """Atomically remove only a source created by the Settings wizard."""
    if not source_id.startswith("sqlite-"):
        raise ValueError("Only user-created sources can be removed")
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"Invalid external source configuration: {exc}") from exc
    if not isinstance(raw, dict) or not isinstance(raw.get("sources"), list):
        raise ValueError("Configuration must contain a sources list")
    if any(
        isinstance(item, dict) and item.get("source_id") == source_id
        for item in raw.get("custom_signals", [])
    ):
        raise ValueError("Source is referenced by a custom signal")
    sources = raw["sources"]
    if any(
        isinstance(item, dict) and item.get("id") == source_id
        and item.get("pending_removal") is True for item in sources
    ):
        raise ValueError("Source has pending removal")
    retained = [
        item
        for item in sources
        if not isinstance(item, dict) or item.get("id") != source_id
    ]
    if len(retained) == len(sources):
        raise ValueError("User-created source was not found")
    raw["sources"] = retained
    temporary = path.with_suffix(path.suffix + ".tmp")
    try:
        temporary.write_text(json.dumps(raw, indent=2) + "\n", encoding="utf-8")
        load_external_sources_configuration(temporary)
        os.replace(temporary, path)
    except OSError as exc:
        raise ValueError(
            f"Could not save external source configuration: {exc}"
        ) from exc


@_locked_configuration_write
def set_user_created_source_enabled(path: Path, source_id: str, enabled: bool) -> None:
    """Atomically change availability independently of synchronization."""
    if not source_id.startswith("sqlite-"):
        raise ValueError("Only user-created sources can be changed")
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"Invalid external source configuration: {exc}") from exc
    if not isinstance(raw, dict) or not isinstance(raw.get("sources"), list):
        raise ValueError("Configuration must contain a sources list")
    found = False
    for item in raw["sources"]:
        if isinstance(item, dict) and item.get("id") == source_id:
            if item.get("pending_removal") is True:
                raise ValueError("Source has pending removal")
            item["enabled"] = enabled
            found = True
    if not found:
        raise ValueError("User-created source was not found")
    temporary = path.with_suffix(path.suffix + ".tmp")
    try:
        temporary.write_text(json.dumps(raw, indent=2) + "\n", encoding="utf-8")
        load_external_sources_configuration(temporary)
        os.replace(temporary, path)
    except OSError as exc:
        raise ValueError(
            f"Could not save external source configuration: {exc}"
        ) from exc


@_locked_configuration_write
def set_source_visibility(path: Path, source_id: str, visible: bool) -> None:
    """Persist visibility while preserving legacy effective synchronization."""
    raw = json.loads(path.read_text(encoding="utf-8"))
    sources = raw.get("sources") if isinstance(raw, dict) else None
    if not isinstance(sources, list):
        raise ValueError("Configuration must contain a sources list")
    matches = [s for s in sources if isinstance(s, dict) and s.get("id") == source_id]
    if len(matches) != 1:
        raise ValueError("Source was not found or is not unique")
    source = matches[0]
    if source.get("pending_removal") is True:
        raise ValueError("Source has pending removal")
    if "visibility_enabled" not in source:
        source["sync_enabled"] = _enabled(source) and _sync_enabled(source)
    source["visibility_enabled"] = visible
    temporary = path.with_suffix(path.suffix + ".tmp")
    try:
        temporary.write_text(json.dumps(raw, indent=2) + "\n", encoding="utf-8")
        load_external_sources_configuration(temporary)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


@_locked_configuration_write
def set_user_created_source_sync_enabled(
    path: Path, source_id: str, enabled: bool
) -> None:
    """Atomically update only the automatic synchronization setting."""
    if not source_id.startswith("sqlite-"):
        raise ValueError("Only user-created sources can be changed")
    raw = json.loads(path.read_text(encoding="utf-8"))
    sources = raw.get("sources") if isinstance(raw, dict) else None
    if not isinstance(sources, list):
        raise ValueError("Configuration must contain a sources list")
    matches = [s for s in sources if isinstance(s, dict) and s.get("id") == source_id]
    if len(matches) != 1:
        raise ValueError("User-created source was not found or is not unique")
    source = matches[0]
    if source.get("pending_removal") is True:
        raise ValueError("Source has pending removal")
    if "visibility_enabled" not in source:
        source["visibility_enabled"] = _enabled(source)
    source["sync_enabled"] = enabled
    temporary = path.with_suffix(path.suffix + ".tmp")
    try:
        temporary.write_text(json.dumps(raw, indent=2) + "\n", encoding="utf-8")
        load_external_sources_configuration(temporary)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


@_locked_configuration_write
def rename_user_created_source(path: Path, source_id: str, display_name: str) -> None:
    """Atomically rename only a source created by the Settings wizard."""
    if not source_id.startswith("sqlite-"):
        raise ValueError("Only user-created sources can be changed")
    name = display_name.strip()
    if not name:
        raise ValueError("Source display name is required")
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"Invalid external source configuration: {exc}") from exc
    if not isinstance(raw, dict) or not isinstance(raw.get("sources"), list):
        raise ValueError("Configuration must contain a sources list")
    found = False
    for item in raw["sources"]:
        if isinstance(item, dict) and item.get("id") == source_id:
            if item.get("pending_removal") is True:
                raise ValueError("Source has pending removal")
            item["display_name"] = name
            found = True
    if not found:
        raise ValueError("User-created source was not found")
    temporary = path.with_suffix(path.suffix + ".tmp")
    try:
        temporary.write_text(json.dumps(raw, indent=2) + "\n", encoding="utf-8")
        load_external_sources_configuration(temporary)
        os.replace(temporary, path)
    except OSError as exc:
        raise ValueError(
            f"Could not save external source configuration: {exc}"
        ) from exc


def validate_new_type_display(name: str, label: str) -> tuple[str, str]:
    """Validate new or edited text while leaving existing configurations readable."""
    name = name.strip()
    if not 1 <= len(name) <= 30:
        raise ValueError("Display name must be 1–30 characters")
    if not re.fullmatch(r"[A-Za-zА-Яа-яЁё0-9]{1,3}", label):
        raise ValueError("Short label must be 1–3 Latin or Cyrillic letters or digits")
    return name, label.upper()


def _validated_display_overrides(
    raw: object, external_registry: SignalTypeRegistry
) -> dict[str, dict[str, str]]:
    if not isinstance(raw, dict):
        raise ValueError("signal_type_display_overrides must be an object")
    if not raw:
        return {}
    registry = external_registry
    types = registry.by_event_type
    validated: dict[str, dict[str, str]] = {}
    for type_id, value in raw.items():
        if not isinstance(type_id, str) or type_id not in types:
            raise ValueError("Unknown signal type ID")
        if not isinstance(value, dict) or set(value) != _DISPLAY_FIELDS:
            raise ValueError("Exactly the five display fields are required")
        if not all(isinstance(v, str) for v in value.values()):
            raise ValueError("Display values must be strings")
        item = {k: v.strip() for k, v in value.items()}
        if not 1 <= len(item["display_name"]) <= 80:
            raise ValueError("Display name must be 1–80 characters")
        if not 1 <= len(item["short_label"]) <= 4:
            raise ValueError("Short label must be 1–4 characters")
        if not re.fullmatch(r"#[0-9a-fA-F]{6}", item["color"]):
            raise ValueError("Color must be a six-digit hex color")
        if item["marker_shape"] not in _SHAPES:
            raise ValueError("Unsupported marker shape")
        if item["marker_position"] not in _POSITIONS:
            raise ValueError("Unsupported marker position")
        validated[type_id] = item
    for type_id, item in validated.items():
        label = item["short_label"].casefold()
        if any(
            other_id != type_id
            and validated.get(other_id, {})
            .get("short_label", other.short_label)
            .strip()
            .casefold()
            == label
            for other_id, other in types.items()
        ):
            raise ValueError("Short label conflicts with another signal type")
    return validated


def registry_with_display_overrides(
    configuration: ExternalSourcesConfiguration,
) -> SignalTypeRegistry:
    """Apply presentation only; preserve type identity and card semantics."""
    types = _registry_with_custom_signals(
        configuration.signal_types, configuration.custom_signals
    ).signal_types
    return SignalTypeRegistry(
        tuple(
            replace(
                item,
                **configuration.signal_type_display_overrides.get(item.event_type, {}),
            )
            for item in types
        )
    )


def _registry_with_custom_signals(
    registry: SignalTypeRegistry,
    custom_signals: tuple[CustomSignalDefinition, ...],
) -> SignalTypeRegistry:
    """Add stable default presentation for configuration-defined custom types."""
    custom_types = tuple(
        SignalType(
            event_type=definition.signal_id,
            filter_key=definition.signal_id,
            short_label=definition.short_label,
            display_name=definition.display_name,
            color="#808080",
            marker_position="aboveBar",
            marker_shape="circle",
            card_tone="external",
        )
        for index, definition in enumerate(custom_signals)
    )
    return SignalTypeRegistry((*registry.signal_types, *custom_types))


@_locked_configuration_write
def save_signal_type_display_override(
    path: Path, type_id: str, values: dict[str, object]
) -> None:
    configuration = load_external_sources_configuration(path)
    raw = json.loads(path.read_text(encoding="utf-8"))
    overrides = raw.setdefault("signal_type_display_overrides", {})
    if not isinstance(overrides, dict):
        raise ValueError("signal_type_display_overrides must be an object")
    registry = registry_with_display_overrides(configuration)
    current = registry.by_event_type.get(type_id)
    if current is None:
        raise ValueError("Unknown signal type ID")
    name = values.get("display_name")
    label = values.get("short_label")
    if not isinstance(name, str) or not isinstance(label, str):
        raise ValueError("Display name and short label are required")
    # Legacy values remain editable for appearance without forcing a rename.
    if name != current.display_name or label != current.short_label:
        checked_name, checked_label = validate_new_type_display(
            name if name != current.display_name else "Valid",
            label if label != current.short_label else "OK",
        )
        values = dict(values)
        if name != current.display_name:
            values["display_name"] = checked_name
        if label != current.short_label:
            values["short_label"] = checked_label
    overrides[type_id] = values
    _validated_display_overrides(
        overrides,
        _registry_with_custom_signals(
            configuration.signal_types, configuration.custom_signals
        ),
    )
    temporary = path.with_suffix(path.suffix + ".tmp")
    try:
        temporary.write_text(json.dumps(raw, indent=2) + "\n", encoding="utf-8")
        load_external_sources_configuration(temporary)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


@_locked_configuration_write
def update_user_created_source_import_types(
    path: Path,
    source_id: str,
    selected_types: list[str],
    type_mapping: dict[str, str],
    display_name: str | None = None,
    new_types: list[dict[str, str]] | None = None,
) -> None:
    """Atomically change a wizard source's future SQLite type selection only."""
    if not source_id.startswith("sqlite-"):
        raise ValueError("Only user-created sources can be changed")
    if not selected_types or len(set(selected_types)) != len(selected_types):
        raise ValueError("Select one or more distinct signal types")
    if set(selected_types) != set(type_mapping) or not all(
        isinstance(item, str) and item for item in selected_types
    ) or not all(isinstance(item, str) and item for item in type_mapping.values()):
        raise ValueError("Every selected signal type requires a dashboard type")
    raw = _read_configuration_object(path)
    registered_types = raw.get("signal_types")
    if not isinstance(registered_types, list):
        raise ValueError("Configuration must contain a signal_types list")
    registered_ids = {
        item.get("id")
        for item in registered_types
        if isinstance(item, dict) and isinstance(item.get("id"), str)
    }
    staged: list[dict[str, str]] = []
    for item in new_types or []:
        if not isinstance(item, dict) or set(item) != {
            "id",
            "display_name",
            "short_label",
        }:
            raise ValueError("Invalid staged signal type")
        type_id = item["id"]
        if not all(isinstance(item[field], str) for field in item):
            raise ValueError("Invalid staged signal type")
        try:
            if (
                not type_id.startswith("custom_")
                or UUID(type_id[7:]).hex != type_id[7:]
            ):
                raise ValueError
        except (ValueError, AttributeError) as exc:
            raise ValueError("Invalid staged signal type ID") from exc
        name, label = validate_new_type_display(
            item["display_name"], item["short_label"]
        )
        if type_id in registered_ids or any(
            staged_item["id"] == type_id for staged_item in staged
        ):
            raise ValueError("Duplicate staged signal type")
        registered_ids.add(type_id)
        staged.append(
            {
                "id": type_id,
                "display_name": name,
                "abbreviation": label,
                "marker_color": "#808080",
                "marker_shape": "circle",
            }
        )
    if not set(type_mapping.values()) <= registered_ids:
        raise ValueError("Signal type destination must be registered")
    if any(item["id"] not in type_mapping.values() for item in staged):
        raise ValueError("Every staged type must be selected by the source")
    sources = raw.get("sources")
    if not isinstance(sources, list):
        raise ValueError("Configuration must contain a sources list")
    source = next(
        (
            item
            for item in sources
            if isinstance(item, dict) and item.get("id") == source_id
        ),
        None,
    )
    if source is None:
        raise ValueError("User-created source was not found")
    if source.get("pending_removal") is True:
        raise ValueError("Source has pending removal")
    if display_name is not None:
        name = display_name.strip()
        if not name:
            raise ValueError("Source display name is required")
        source["display_name"] = name
    existing_mapping = source.get("type_mapping")
    if not isinstance(existing_mapping, dict):
        raise ValueError("Source type mapping is invalid")
    if any(
        source_type in existing_mapping
        and existing_mapping[source_type] != destination_type
        for source_type, destination_type in type_mapping.items()
    ):
        raise ValueError("Existing source type mappings cannot be changed here")
    # Retain old mappings: saved card preferences can still refer to a deselected
    # destination type, and only selected_types controls future imports.
    source["type_mapping"] = {**existing_mapping, **type_mapping}
    source["selected_types"] = selected_types
    registered_types.extend(staged)
    _save_configuration_object(path, raw)


@_locked_configuration_write
def save_signal_card_layout(
    path: Path,
    source_id: str,
    type_id: str,
    layout: dict[str, object],
) -> SignalCardLayout:
    """Atomically save one source-scoped, destination-type card layout."""
    if not isinstance(layout, dict):
        raise ValueError("Card layout must be an object")
    configuration = load_external_sources_configuration(path)
    source = _card_layout_source(configuration, source_id, type_id)
    raw = json.loads(path.read_text(encoding="utf-8"))
    sources = raw.get("sources") if isinstance(raw, dict) else None
    if not isinstance(sources, list):
        raise ValueError("Configuration must contain a sources list")
    raw_source = next(
        (
            item
            for item in sources
            if isinstance(item, dict) and item.get("id") == source.source_id
        ),
        None,
    )
    if raw_source is None:
        raise ValueError("Source was not found")
    layouts = raw_source.get("card_layouts", {})
    if not isinstance(layouts, dict):
        raise ValueError("card_layouts must be an object")
    layouts[type_id] = layout
    raw_source["card_layouts"] = layouts
    temporary = path.with_suffix(path.suffix + ".tmp")
    try:
        temporary.write_text(json.dumps(raw, indent=2) + "\n", encoding="utf-8")
        saved = load_external_sources_configuration(temporary)
        saved_layout = _card_layout_source(saved, source_id, type_id).card_layouts[
            type_id
        ]
        os.replace(temporary, path)
        return saved_layout
    except OSError as exc:
        raise ValueError(
            f"Could not save external source configuration: {exc}"
        ) from exc
    finally:
        temporary.unlink(missing_ok=True)


def _card_layout_source(
    configuration: ExternalSourcesConfiguration, source_id: str, type_id: str
) -> ExternalSqliteSource:
    source = next(
        (item for item in configuration.sources if item.source_id == source_id), None
    )
    if source is None:
        raise ValueError("Source was not found")
    custom_ids = {
        item.signal_id
        for item in configuration.custom_signals
        if item.source_id == source_id
    }
    if type_id not in source.type_mapping.values() and type_id not in custom_ids:
        raise ValueError("Signal type is not mapped by this source")
    return source


def _signal_type(raw: object) -> SignalType:
    item = _object(raw, "signal type")
    event_type = _string(item, "id")
    short = _string(item, "abbreviation")
    shape = _string(item, "marker_shape")
    color = _string(item, "marker_color")
    if not 1 <= len(short) <= 4 or shape not in _SHAPES or not color.startswith("#"):
        raise ValueError("Invalid signal appearance")
    return SignalType(
        event_type,
        event_type,
        short,
        _string(item, "display_name"),
        color,
        "aboveBar",
        shape,
        "external",
    )


def _source(raw: object, base: Path) -> ExternalSqliteSource:
    item = _object(raw, "source")
    pending_removal = item.get("pending_removal", False)
    if not isinstance(pending_removal, bool):
        raise ValueError("pending_removal must be a boolean")
    timezone_name = item.get("timezone")
    try:
        timezone = None if timezone_name is None else ZoneInfo(str(timezone_name))
    except ZoneInfoNotFoundError as exc:
        raise ValueError("Invalid source timezone") from exc
    database = Path(_string(item, "database_path"))
    if not database.is_absolute():
        database = base.resolve() / database
    database = database.resolve()
    fixed_signal_type = _optional_string(item, "fixed_signal_type")
    mapping = item.get("type_mapping")
    if not isinstance(mapping, dict) or not all(
        isinstance(key, str) and isinstance(value, str)
        for key, value in mapping.items()
    ):
        raise ValueError("type_mapping must be a string mapping")
    return ExternalSqliteSource(
        source_id=_string(item, "id"),
        source_origin=_optional_string(item, "source_origin"),
        pending_removal=pending_removal,
        display_name=_string(item, "display_name"),
        database_path=database,
        table_name=_string(item, "table"),
        primary_key_column=_string(item, "primary_key_column"),
        symbol_column=_string(item, "symbol_column"),
        timestamp_column=_string(item, "timestamp_column"),
        signal_type_column=(
            None
            if fixed_signal_type is not None
            else _string(item, "signal_type_column")
        ),
        price_column=_optional_string(item, "price_column"),
        allowed_source_types=frozenset(_strings(item, "selected_types")),
        type_mapping=mapping,
        source_timezone=timezone,
        sync_interval_seconds=_interval(item),
        exchange=(
            str(item["exchange"]) if item.get("exchange") is not None else "BYBIT"
        ),
        enabled=_enabled(item),
        sync_enabled=_sync_enabled(item),
        visibility_enabled=_visibility_enabled(item),
        fixed_signal_type=fixed_signal_type,
        metadata_columns=_metadata_columns(item),
        retain_all_columns=item.get("retain_all_columns") is True,
        exchange_column=_optional_string(item, "exchange_column"),
        card_layouts=_card_layouts(item, set(mapping.values())),
    )


def _card_layouts(
    item: dict[str, object], destination_types: set[str]
) -> dict[str, SignalCardLayout]:
    raw_layouts = item.get("card_layouts", {})
    if not isinstance(raw_layouts, dict):
        raise ValueError("card_layouts must be an object")
    layouts: dict[str, SignalCardLayout] = {}
    for type_id, raw_layout in raw_layouts.items():
        if not isinstance(type_id, str):
            raise ValueError("Card layout signal type must be a string")
        if not isinstance(raw_layout, dict) or set(raw_layout) != {
            "compact_fields",
            "detail_fields",
        }:
            raise ValueError("Card layout requires compact_fields and detail_fields")
        compact = _card_field_descriptors(
            raw_layout["compact_fields"], "compact_fields"
        )
        if len(compact) > 3:
            raise ValueError(
                "Card layout compact_fields may contain at most three fields"
            )
        detail = _card_field_descriptors(raw_layout["detail_fields"], "detail_fields")
        fields = (*compact, *detail)
        if len(fields) != len(set(fields)):
            raise ValueError("Card layout fields must not contain duplicates")
        layouts[type_id] = SignalCardLayout(
            compact_fields=tuple(compact),
            detail_fields=tuple(detail),
        )
    return layouts


def _card_field_descriptors(raw: object, name: str) -> tuple[str, ...]:
    if not isinstance(raw, list) or not all(isinstance(value, str) for value in raw):
        raise ValueError(f"Card layout {name} must be a string list")
    descriptors = tuple(raw)
    for descriptor in descriptors:
        namespace, separator, field_name = descriptor.partition(":")
        if not separator or not field_name or "\x00" in field_name:
            raise ValueError("Invalid card field descriptor")
        if namespace == "standard":
            if field_name not in CARD_LAYOUT_STANDARD_FIELDS:
                raise ValueError("Unknown standard card field")
        elif namespace != "metadata":
            raise ValueError("Invalid card field descriptor")
    return descriptors


def _metadata_columns(item: dict[str, object]) -> dict[str, str]:
    mappings = item.get("field_mappings")
    if mappings is None:
        return {}
    if not isinstance(mappings, dict):
        raise ValueError("field_mappings must be an object")
    metadata: dict[str, str] = {}
    for name, mapping in mappings.items():
        if not isinstance(name, str) or not name.startswith("metadata:"):
            continue
        metadata_name = name.removeprefix("metadata:")
        if (
            not metadata_name
            or not isinstance(mapping, dict)
            or mapping.get("kind") != "column"
            or not isinstance(mapping.get("column"), str)
        ):
            raise ValueError("Invalid metadata column mapping")
        metadata[metadata_name] = mapping["column"]
    return metadata


def _list(raw: dict[str, object], name: str) -> list[object]:
    value = raw.get(name)
    if not isinstance(value, list):
        raise ValueError(f"{name} must be a list")
    return value


def _object(raw: object, name: str) -> dict[str, object]:
    if not isinstance(raw, dict):
        raise ValueError(f"{name} must be an object")
    return raw


def _string(raw: dict[str, object], name: str) -> str:
    value = raw.get(name)
    if not isinstance(value, str) or not value:
        raise ValueError(f"{name} is required")
    return value


def _optional_string(raw: dict[str, object], name: str) -> str | None:
    value = raw.get(name)
    if value is None:
        return None
    if not isinstance(value, str) or not value:
        raise ValueError(f"{name} must be a string")
    return value


def _strings(raw: dict[str, object], name: str) -> list[str]:
    value = raw.get(name)
    if (
        not isinstance(value, list)
        or not value
        or not all(isinstance(item, str) for item in value)
    ):
        raise ValueError(f"{name} must be a non-empty string list")
    return value


def _interval(raw: dict[str, object]) -> int:
    value = raw.get("sync_interval_seconds", 30)
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise ValueError("sync_interval_seconds must be a positive integer")
    return value


def _enabled(raw: dict[str, object]) -> bool:
    value = raw.get("enabled", True)
    if not isinstance(value, bool):
        raise ValueError("enabled must be a boolean")
    return value


def _sync_enabled(raw: dict[str, object]) -> bool:
    value = raw.get("sync_enabled", False)
    if not isinstance(value, bool):
        raise ValueError("sync_enabled must be a boolean")
    return value


def _visibility_enabled(raw: dict[str, object]) -> bool | None:
    if "visibility_enabled" not in raw:
        return None
    value = raw["visibility_enabled"]
    if not isinstance(value, bool):
        raise ValueError("visibility_enabled must be a boolean")
    return value
