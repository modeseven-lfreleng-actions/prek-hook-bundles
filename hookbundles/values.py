# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""Narrow untyped data, read from JSON, YAML and TOML, to known shapes.

Everything the builder parses is data somebody else wrote: a
repository's configuration, a marker a hook's install code may have
replaced, a manifest. Each helper returns the value when it has the
expected shape and None otherwise, so a caller decides what a wrong
shape means instead of an exception deciding for it.
"""

from __future__ import annotations

import json
from typing import cast


def parse(text: str | bytes) -> object:
    """Parse JSON, raising ValueError when it is not JSON."""
    return cast("object", json.loads(text))


def mapping(value: object) -> dict[str, object] | None:
    """Return a mapping with string keys, or None."""
    if not isinstance(value, dict):
        return None
    items = cast("dict[object, object]", value)
    result: dict[str, object] = {}
    for key, item in items.items():
        if not isinstance(key, str):
            return None
        result[key] = item
    return result


def sequence(value: object) -> list[object] | None:
    """Return a list, or None."""
    if not isinstance(value, list):
        return None
    return cast("list[object]", value)


def strings(value: object) -> tuple[str, ...] | None:
    """Return a list of strings as a tuple, or None."""
    items = sequence(value)
    if items is None:
        return None
    result: list[str] = []
    for item in items:
        if not isinstance(item, str):
            return None
        result.append(item)
    return tuple(result)


def string(value: object) -> str | None:
    """Return a string, or None."""
    return value if isinstance(value, str) else None


def integer(value: object) -> int | None:
    """Return an integer that is not a boolean, or None."""
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value
