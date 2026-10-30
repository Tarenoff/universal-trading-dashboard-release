"""Signal-type presentation and URL metadata for dashboard instances."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True, slots=True)
class SignalType:
    event_type: str
    filter_key: str
    short_label: str
    display_name: str
    color: str
    marker_position: str
    marker_shape: str
    card_tone: str

    def frontend_definition(self) -> dict[str, Any]:
        collapsed_fields = {
            "pump_reversal": ["alert_level", "source_interval"],
            "pump_pullback": ["alert_level", "direction"],
            "test_breakout": ["alert_level", "source_interval"],
        }.get(self.event_type, ["alert_level", "source_interval"])
        return {
            "event_type": self.event_type,
            "filter_key": self.filter_key,
            "short_label": self.short_label,
            "display_name": self.display_name,
            "color": self.color,
            "chart_marker": {
                "position": self.marker_position,
                "shape": self.marker_shape,
            },
            "event_card": {
                "tone": self.card_tone,
                "collapsed_fields": collapsed_fields,
            },
        }


@dataclass(frozen=True, slots=True)
class SignalTypeRegistry:
    signal_types: tuple[SignalType, ...]

    def __post_init__(self) -> None:
        event_types = [item.event_type for item in self.signal_types]
        filter_keys = [item.filter_key for item in self.signal_types]
        if len(event_types) != len(set(event_types)) or len(filter_keys) != len(
            set(filter_keys)
        ):
            raise ValueError("Signal types and filter keys must be unique")

    @property
    def by_event_type(self) -> dict[str, SignalType]:
        return {item.event_type: item for item in self.signal_types}

    @property
    def by_filter_key(self) -> dict[str, SignalType]:
        return {item.filter_key: item for item in self.signal_types}

    def frontend_definitions(self) -> dict[str, dict[str, Any]]:
        return {
            item.event_type: item.frontend_definition() for item in self.signal_types
        }


PRODUCTION_SIGNAL_TYPE_REGISTRY = SignalTypeRegistry(
    (
        SignalType(
            "pump_reversal",
            "pr",
            "PR",
            "Pump Reversal",
            "#f2b84b",
            "aboveBar",
            "arrowDown",
            "pr",
        ),
        SignalType(
            "pump_pullback",
            "pb",
            "PB",
            "Pump Pullback",
            "#9b7bff",
            "belowBar",
            "arrowUp",
            "pb",
        ),
    )
)
