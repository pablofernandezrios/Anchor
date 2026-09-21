"""A small declarative validator for IPC payloads (SPEC 5.2).

The specification requires schema validation and the rejection of unknown
fields, and root daemons may not use pip packages (SPEC 5.4), so this replaces
a third-party validation library with the little that Anchor actually needs:
typed fields, required and optional values, choices, and bounds.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from anchor.protocol.errors import ProtocolError

_TYPE_NAMES: dict[type, str] = {
    str: "a string",
    int: "an integer",
    float: "a number",
    bool: "a boolean",
    dict: "an object",
    list: "an array",
}


@dataclass(frozen=True, slots=True)
class Field:
    """One key of a payload object."""

    kind: type
    required: bool = True
    default: Any = None
    choices: Sequence[str] | None = None
    minimum: int | None = None
    maximum: int | None = None
    item_kind: type | None = None
    convert: Callable[[Any], Any] | None = None
    nested: Schema | None = None
    """For an object field, the schema its own keys must satisfy.

    Without it a nested object would be the one place a typo passes silently,
    which is exactly what SPEC 5.2 asks the validator to prevent.
    """

    def _describe(self) -> str:
        return _TYPE_NAMES.get(self.kind, self.kind.__name__)

    def validate(self, name: str, value: Any) -> Any:
        # bool is a subclass of int, so an explicit check keeps `true` out of
        # integer fields and `1` out of boolean ones.
        if self.kind is bool:
            if not isinstance(value, bool):
                raise ProtocolError(f"field {name!r} must be {self._describe()}")
        elif self.kind is float:
            if isinstance(value, bool) or not isinstance(value, int | float):
                raise ProtocolError(f"field {name!r} must be {self._describe()}")
            value = float(value)
        elif self.kind is int:
            if isinstance(value, bool) or not isinstance(value, int):
                raise ProtocolError(f"field {name!r} must be {self._describe()}")
        elif not isinstance(value, self.kind):
            raise ProtocolError(f"field {name!r} must be {self._describe()}")

        if self.choices is not None and value not in self.choices:
            allowed = ", ".join(repr(choice) for choice in self.choices)
            raise ProtocolError(f"field {name!r} must be one of: {allowed}")

        if isinstance(value, int | float) and not isinstance(value, bool):
            if self.minimum is not None and value < self.minimum:
                raise ProtocolError(f"field {name!r} must be at least {self.minimum}")
            if self.maximum is not None and value > self.maximum:
                raise ProtocolError(f"field {name!r} must be at most {self.maximum}")

        if self.kind is list and self.item_kind is not None:
            for index, item in enumerate(value):
                if not isinstance(item, self.item_kind):
                    expected = _TYPE_NAMES.get(self.item_kind, self.item_kind.__name__)
                    raise ProtocolError(f"item {index} of field {name!r} must be {expected}")

        if self.kind is dict and self.nested is not None:
            value = self.nested.validate(value, context=f"field {name!r}")

        if self.convert is not None:
            try:
                return self.convert(value)
            except ProtocolError:
                raise
            except (TypeError, ValueError) as exc:
                raise ProtocolError(f"field {name!r} is not valid: {exc}") from exc
        return value


class Schema:
    """A set of named fields describing one payload object."""

    __slots__ = ("fields",)

    def __init__(self, **fields: Field) -> None:
        self.fields = fields

    def validate(self, payload: Mapping[str, Any], *, context: str = "payload") -> dict[str, Any]:
        """Return a validated copy of ``payload``.

        Unknown fields are rejected rather than ignored, so a typo in a client
        fails loudly instead of silently doing nothing (SPEC 5.2).
        """
        if not isinstance(payload, Mapping):
            raise ProtocolError(f"{context} must be an object")

        unknown = sorted(set(payload) - set(self.fields))
        if unknown:
            names = ", ".join(repr(key) for key in unknown)
            raise ProtocolError(f"{context} has unknown fields: {names}")

        result: dict[str, Any] = {}
        for name, field in self.fields.items():
            if name not in payload:
                if field.required:
                    raise ProtocolError(f"{context} is missing required field {name!r}")
                result[name] = field.default
                continue
            result[name] = field.validate(name, payload[name])
        return result


EMPTY = Schema()
