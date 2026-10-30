"""Built-in signal registry used by portable source-removal checks."""

from dashboard.signal_type_registry import (
    PRODUCTION_SIGNAL_TYPE_REGISTRY,
    SignalType,
    SignalTypeRegistry,
)

BUILT_IN_SIGNAL_TYPE_REGISTRY = SignalTypeRegistry(
    PRODUCTION_SIGNAL_TYPE_REGISTRY.signal_types
    + (
        SignalType(
            "test_breakout", "bo", "BO", "EXTREME 60m", "#3B82F6",
            "aboveBar", "circle", "bo",
        ),
    )
)
