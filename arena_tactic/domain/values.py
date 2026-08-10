"""Safe immutable values shared by Phase-one domain records."""

from __future__ import annotations

import math
import re
from collections.abc import Mapping
from enum import Enum
from types import MappingProxyType
from typing import TypeAlias


JsonScalar: TypeAlias = None | bool | int | float | str
FrozenJson: TypeAlias = JsonScalar | tuple["FrozenJson", ...] | Mapping[str, "FrozenJson"]

_FULL_UUID = re.compile(
    r"(?<![0-9a-fA-F])[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}(?![0-9a-fA-F])"
)
_SENSITIVE_KEYS = {
    "authorization", "api_key", "apikey", "controller", "cookie", "password", "secret", "token",
}
_SENSITIVE_NORMALIZED = frozenset(re.sub(r"[-_]+", "", item) for item in _SENSITIVE_KEYS)


def _normalized_key(value: str) -> str:
    """Normalize spelling variants without weakening the stored key contract."""
    return re.sub(r"[-_]+", "", value.strip().casefold())


def _is_sdk_or_controller(value: object) -> bool:
    value_type = type(value)
    module = value_type.__module__.split(".", 1)[0]
    name = value_type.__name__.casefold().replace("_", "")
    return module == "arena_hero" or "controller" in name


def freeze_json(value: object, *, field_name: str) -> FrozenJson:
    """Deep-copy JSON-like input into immutable, credential-free values."""
    if isinstance(value, Enum) or _is_sdk_or_controller(value):
        raise TypeError(f"{field_name} contains an enum or SDK object")
    if value is None or isinstance(value, (bool, int)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError(f"{field_name} contains a non-finite float")
        return value
    if isinstance(value, str):
        if _FULL_UUID.search(value):
            raise ValueError(f"{field_name} contains a full UUID")
        return value
    if isinstance(value, Mapping):
        copied: dict[str, FrozenJson] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise TypeError(f"{field_name} mapping keys must be strings")
            normalized = _normalized_key(key)
            parts = set(re.split(r"[-_]+", key.strip().casefold()))
            if normalized in _SENSITIVE_NORMALIZED or parts & _SENSITIVE_KEYS:
                raise ValueError(f"{field_name} contains sensitive key {key!r}")
            copied[key] = freeze_json(item, field_name=f"{field_name}.{key}")
        return MappingProxyType(copied)
    if isinstance(value, (list, tuple)):
        return tuple(freeze_json(item, field_name=field_name) for item in value)
    raise TypeError(f"{field_name} contains unsupported {type(value).__name__}")


def freeze_mapping(value: Mapping[str, object], *, field_name: str) -> Mapping[str, FrozenJson]:
    frozen = freeze_json(value, field_name=field_name)
    assert isinstance(frozen, Mapping)
    return frozen


def freeze_text(value: object, *, field_name: str) -> str:
    """Validate a direct persisted text field with the shared safe-value rules."""
    frozen = freeze_json(value, field_name=field_name)
    if not isinstance(frozen, str):
        raise TypeError(f"{field_name} must be a string")
    return frozen


def freeze_optional_text(value: object, *, field_name: str) -> str | None:
    if value is None:
        return None
    return freeze_text(value, field_name=field_name)


def freeze_sequence(value: object, *, field_name: str) -> tuple[FrozenJson, ...]:
    """Deep-copy a JSON-like list/tuple and require a sequence at the boundary."""
    if not isinstance(value, (list, tuple)):
        raise TypeError(f"{field_name} must be a list or tuple")
    frozen = freeze_json(value, field_name=field_name)
    assert isinstance(frozen, tuple)
    return frozen


def thaw_json(value: FrozenJson) -> JsonScalar | list[object] | dict[str, object]:
    """Return ordinary JSON containers for serialization."""
    if isinstance(value, Mapping):
        return {key: thaw_json(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [thaw_json(item) for item in value]
    return value
