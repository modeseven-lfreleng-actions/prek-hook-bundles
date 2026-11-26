# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""The builder's settings, read from bundle.toml."""

from __future__ import annotations

import tomllib
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from hookbundles import values


class SettingsError(Exception):
    """bundle.toml is missing a value or holds one of the wrong shape."""


@dataclass(frozen=True)
class Settings:
    """What to survey, and what to build against."""

    org: str
    min_repos: int
    always: tuple[str, ...]
    prek_home: str
    image_os: str
    python: str
    node: str
    golang: str


def _table(data: dict[str, object], name: str) -> dict[str, object]:
    table = values.mapping(data.get(name))
    if table is None:
        raise SettingsError(f"[{name}] is missing or is not a table")
    return table


def _string(table: dict[str, object], section: str, key: str) -> str:
    value = values.string(table.get(key))
    if value is None:
        raise SettingsError(f"[{section}] {key} must be a string")
    return value


def _strings(table: dict[str, object], section: str, key: str) -> tuple[str, ...]:
    value = values.strings(table.get(key))
    if value is None:
        raise SettingsError(f"[{section}] {key} must be a list of strings")
    return value


def load(path: Path) -> Settings:
    """Read and check the settings file at path."""
    with path.open("rb") as handle:
        data = values.mapping(tomllib.load(handle))
    if data is None:
        raise SettingsError(f"{path} is not a table")
    sources = _table(data, "sources")
    platform = _table(data, "platform")
    toolchains = _table(data, "toolchains")

    min_repos = values.integer(sources.get("min_repos"))
    if min_repos is None or min_repos < 1:
        raise SettingsError("[sources] min_repos must be a positive integer")
    prek_home = _string(platform, "platform", "prek_home")
    home = PurePosixPath(prek_home)
    if not home.is_absolute() or ".." in home.parts or str(home) != prek_home:
        raise SettingsError("[platform] prek_home must be a normalised absolute path")
    python = _string(toolchains, "toolchains", "python")
    if not python:
        raise SettingsError("[toolchains] python must name a version")
    return Settings(
        org=_string(sources, "sources", "org"),
        min_repos=min_repos,
        always=_strings(sources, "sources", "always"),
        prek_home=prek_home,
        image_os=_string(platform, "platform", "image_os"),
        python=python,
        node=_string(toolchains, "toolchains", "node"),
        golang=_string(toolchains, "toolchains", "golang"),
    )
