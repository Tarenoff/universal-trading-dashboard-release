"""Pure configuration-backed classification for derived dashboard signals."""

from __future__ import annotations

import json
import math
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace

from dashboard.event_store import CARD_LAYOUT_STANDARD_FIELD_NAMES, SignalEvent

type CustomSignalValue = str | int | float | bool
_CUSTOM_SIGNAL_ID = re.compile(r"[A-Za-z][A-Za-z0-9_]{0,79}")
_OPERATORS = frozenset({"=", ">=", "<="})


@dataclass(frozen=True, slots=True)
class CustomSignalCondition:
    """One validated predicate over an imported event field."""

    descriptor: str
    operator: str
    value: CustomSignalValue


@dataclass(frozen=True, slots=True)
class CustomSignalDefinition:
    """A visible-on-read signal type derived from one configured source."""

    signal_id: str
    display_name: str
    short_label: str
    source_id: str
    enabled: bool
    conditions: tuple[CustomSignalCondition, ...]


def parse_custom_signals(
    raw: object,
    *,
    source_ids: set[str],
    existing_type_ids: set[str],
) -> tuple[CustomSignalDefinition, ...]:
    """Validate persisted definitions while retaining their declaration order."""
    if raw is None:
        return ()
    if not isinstance(raw, list):
        raise ValueError("custom_signals must be a list")
    definitions = tuple(_custom_signal(item, source_ids) for item in raw)
    ids = [item.signal_id for item in definitions]
    if len(ids) != len(set(ids)):
        raise ValueError("Custom signal IDs must be unique")
    collision = set(ids) & existing_type_ids
    if collision:
        raise ValueError("Custom signal ID conflicts with a source signal type")
    return definitions


def project_visible_events(
    events: Sequence[SignalEvent],
    *,
    source_visibility: Mapping[str, bool],
    custom_signals: Sequence[CustomSignalDefinition],
) -> list[SignalEvent]:
    """Return visible display projections without modifying stored events.

    A visible custom signal takes precedence over its source event. Source
    visibility is considered only after custom matching, so hidden sources can
    still supply visible custom projections from existing EventStore rows.
    """
    result: list[SignalEvent] = []
    for event in events:
        matched = next(
            (
                definition
                for definition in custom_signals
                if definition.enabled and event_matches_custom_signal(event, definition)
            ),
            None,
        )
        if matched is not None:
            result.append(replace(event, event_type=matched.signal_id))
        elif event.owner_source_id is None or source_visibility.get(
            event.owner_source_id, True
        ):
            result.append(event)
    return result


def event_matches_custom_signal(
    event: SignalEvent, definition: CustomSignalDefinition
) -> bool:
    """Return whether every configured condition matches one owned event."""
    if event.owner_source_id != definition.source_id:
        return False
    metadata = _event_metadata(event)
    return all(
        _condition_matches(
            _event_value(event, metadata, condition.descriptor), condition
        )
        for condition in definition.conditions
    )


def _custom_signal(raw: object, source_ids: set[str]) -> CustomSignalDefinition:
    if not isinstance(raw, dict):
        raise ValueError("Custom signal must be an object")
    if set(raw) - {
        "id",
        "display_name",
        "short_label",
        "source_id",
        "enabled",
        "conditions",
    }:
        raise ValueError("Custom signal has unsupported fields")
    signal_id = raw.get("id")
    display_name = raw.get("display_name")
    short_label = raw.get("short_label", "CS")
    source_id = raw.get("source_id")
    enabled = raw.get("enabled", True)
    conditions = raw.get("conditions")
    if not isinstance(signal_id, str) or not _CUSTOM_SIGNAL_ID.fullmatch(signal_id):
        raise ValueError("Invalid custom signal ID")
    if not isinstance(display_name, str) or not 1 <= len(display_name.strip()) <= 80:
        raise ValueError("Custom signal display name must be 1–80 characters")
    if not isinstance(short_label, str) or not re.fullmatch(
        r"[A-Za-zА-Яа-яЁё0-9]{1,3}", short_label
    ):
        raise ValueError(
            "Custom signal label must be 1–3 Latin or Cyrillic letters or digits"
        )
    if not isinstance(source_id, str) or source_id not in source_ids:
        raise ValueError("Custom signal source_id must reference a configured source")
    if not isinstance(enabled, bool):
        raise ValueError("Custom signal enabled must be a boolean")
    if not isinstance(conditions, list) or not conditions:
        raise ValueError("Custom signal requires at least one condition")
    parsed_conditions = tuple(_condition(item) for item in conditions)
    if len({item.descriptor for item in parsed_conditions}) != len(parsed_conditions):
        raise ValueError("Custom signal conditions must use unique fields")
    return CustomSignalDefinition(
        signal_id=signal_id,
        display_name=display_name.strip(),
        short_label=short_label.upper(),
        source_id=source_id,
        enabled=enabled,
        conditions=parsed_conditions,
    )


def _condition(raw: object) -> CustomSignalCondition:
    if not isinstance(raw, dict) or set(raw) != {"field", "operator", "value"}:
        raise ValueError("Custom signal condition requires field, operator and value")
    descriptor = raw["field"]
    operator = raw["operator"]
    value = raw["value"]
    if not isinstance(descriptor, str) or not _valid_descriptor(descriptor):
        raise ValueError("Invalid custom signal condition field")
    if not isinstance(operator, str) or operator not in _OPERATORS:
        raise ValueError("Unsupported custom signal condition operator")
    if not _valid_scalar(value):
        raise ValueError("Custom signal condition value must be a finite scalar")
    if operator != "=" and not _valid_numeric(value):
        raise ValueError("Numeric custom signal comparisons require a finite number")
    return CustomSignalCondition(descriptor, operator, value)


def _valid_descriptor(value: str) -> bool:
    namespace, separator, field_name = value.partition(":")
    if not separator or not field_name or "\x00" in field_name:
        return False
    return (
        namespace == "standard" and field_name in CARD_LAYOUT_STANDARD_FIELD_NAMES
    ) or namespace == "metadata"


def _valid_scalar(value: object) -> bool:
    if isinstance(value, bool) or isinstance(value, str) or isinstance(value, int):
        return True
    return isinstance(value, float) and math.isfinite(value)


def _event_metadata(event: SignalEvent) -> Mapping[str, object]:
    try:
        parsed = json.loads(event.metadata_json)
    except (TypeError, ValueError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _event_value(
    event: SignalEvent, metadata: Mapping[str, object], descriptor: str
) -> object:
    namespace, field_name = descriptor.split(":", 1)
    if namespace == "metadata":
        return metadata.get(field_name)
    return getattr(event, field_name)


def _values_equal(actual: object, expected: CustomSignalValue) -> bool:
    """Compare JSON scalars without treating boolean values as integers."""
    if isinstance(actual, bool) != isinstance(expected, bool):
        return False
    if isinstance(actual, (int, float)) and isinstance(expected, (int, float)):
        return (
            not isinstance(actual, bool)
            and not isinstance(expected, bool)
            and actual == expected
        )
    if isinstance(actual, str) and isinstance(expected, (int, float)):
        try:
            return float(actual) == expected
        except ValueError:
            return False
    return type(actual) is type(expected) and actual == expected


def _condition_matches(actual: object, condition: CustomSignalCondition) -> bool:
    if condition.operator == "=":
        return _values_equal(actual, condition.value)
    expected = _numeric_value(condition.value)
    received = _numeric_value(actual)
    if expected is None or received is None:
        return False
    if condition.operator == ">=":
        return received >= expected
    return received <= expected


def _valid_numeric(value: object) -> bool:
    return _numeric_value(value) is not None


def _numeric_value(value: object) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        number = float(value)
    elif isinstance(value, str):
        try:
            number = float(value)
        except ValueError:
            return None
    else:
        return None
    return number if math.isfinite(number) else None
