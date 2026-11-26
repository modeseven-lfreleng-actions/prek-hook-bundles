# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""Turn the organisation's configurations into units to build.

A unit is one hook environment requirement, as prek compares them: the
hook repository and revision, the additional dependencies in the order
given, and the language version asked for. Two repositories whose
configurations differ still share a unit wherever those agree, which
is what makes one bundle serve the estate.

The planner treats every configuration as data. It never clones a
repository or runs anything a configuration names.
"""

from __future__ import annotations

import hashlib
import importlib
import json
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, cast

from hookbundles import values

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable

    from hookbundles.settings import Settings

# The languages whose toolchain the base stage seeds. A hook in any
# other language builds its environment on the runner, as it does now.
SEEDED = ("python", "node", "golang", "pygrep")

# Languages for which prek installs no environment at all.
NO_ENVIRONMENT = frozenset({"system", "script", "fail", "docker_image"})

# A full commit id. A tag or a branch can move, so an environment built
# from one could differ from the one a later run would build.
_COMMIT = re.compile(r"[0-9a-f]{40}(?:[0-9a-f]{24})?")

# Larger than any configuration in the organisation by a wide margin.
_MAX_CONFIG_BYTES = 512 * 1024


@dataclass(frozen=True)
class Unit:
    """One hook environment requirement, and who asks for it."""

    repo: str
    rev: str
    language: str
    dependencies: tuple[str, ...]
    language_version: str
    hooks: tuple[str, ...]
    users: tuple[str, ...]

    @property
    def local(self) -> bool:
        """Whether the hook is defined in the configuration itself."""
        return self.repo == "local"

    @property
    def uid(self) -> str:
        """A stable identifier for the requirement, safe as a file name."""
        key = [
            self.repo,
            self.rev,
            self.language,
            list(self.dependencies),
            self.language_version,
        ]
        digest = hashlib.sha256(json.dumps(key).encode()).hexdigest()
        return digest[:16]

    @property
    def label(self) -> str:
        """A short description for logs and summaries."""
        if self.local:
            return f"local {self.language} {' '.join(self.dependencies)}"
        return f"{self.repo}@{self.rev[:12]}"

    def config(self) -> dict[str, object]:
        """Return the configuration that makes prek build this unit."""
        options: dict[str, object] = {}
        if self.dependencies:
            options["additional_dependencies"] = list(self.dependencies)
        if self.language_version:
            options["language_version"] = self.language_version
        if self.local:
            hook: dict[str, object] = {
                "id": "bundle",
                "name": "bundle",
                "language": self.language,
                "entry": "true",
                **options,
            }
            return {"repos": [{"repo": "local", "hooks": [hook]}]}
        if self.language:
            options["language"] = self.language
        hooks: list[object] = [{"id": name, **options} for name in self.hooks]
        return {"repos": [{"repo": self.repo, "rev": self.rev, "hooks": hooks}]}

    def to_json(self) -> dict[str, object]:
        """Return the unit as JSON-ready data."""
        return {
            "repo": self.repo,
            "rev": self.rev,
            "language": self.language,
            "dependencies": list(self.dependencies),
            "language_version": self.language_version,
            "hooks": list(self.hooks),
            "users": list(self.users),
        }

    @staticmethod
    def from_json(data: object) -> Unit:
        """Rebuild a unit from to_json output."""
        fields = values.mapping(data)
        if fields is None:
            raise ValueError("a unit must be a mapping")
        texts = {
            key: values.string(fields.get(key))
            for key in ("repo", "rev", "language", "language_version")
        }
        lists = {
            key: values.strings(fields.get(key))
            for key in ("dependencies", "hooks", "users")
        }
        if None in texts.values() or None in lists.values():
            raise ValueError("a unit field is missing or has the wrong type")
        return Unit(
            repo=texts["repo"] or "",
            rev=texts["rev"] or "",
            language=texts["language"] or "",
            dependencies=lists["dependencies"] or (),
            language_version=texts["language_version"] or "",
            hooks=lists["hooks"] or (),
            users=lists["users"] or (),
        )


@dataclass(frozen=True)
class Plan:
    """The units to build, and what the survey left out and why."""

    units: tuple[Unit, ...]
    notes: tuple[str, ...]
    surveyed: int

    def to_json(self) -> dict[str, object]:
        """Return the plan as JSON-ready data."""
        return {
            "surveyed": self.surveyed,
            "units": [unit.to_json() for unit in self.units],
            "notes": list(self.notes),
        }

    @staticmethod
    def from_json(data: object) -> Plan:
        """Rebuild a plan from to_json output."""
        fields = values.mapping(data)
        units = values.sequence(fields.get("units")) if fields else None
        notes = values.strings(fields.get("notes")) if fields else None
        surveyed = values.integer(fields.get("surveyed")) if fields else None
        if units is None or notes is None or surveyed is None:
            raise ValueError("the plan is missing a field")
        return Plan(
            units=tuple(Unit.from_json(unit) for unit in units),
            notes=notes,
            surveyed=surveyed,
        )


# repo, rev, language, dependencies, language_version
Key = tuple[str, str, str, tuple[str, ...], str]


def is_seed(key: Key) -> bool:
    """Whether a key is a local hook with no dependencies.

    The base stage builds that environment for every seeded language,
    so it is in every bundle and is never a unit.
    """
    return key[0] == "local" and not key[3] and not key[4]


def key_json(key: Key) -> dict[str, object]:
    """Return a key as the bundle index records a requirement."""
    return {
        "repo": key[0],
        "rev": key[1],
        "language": key[2],
        "dependencies": list(key[3]),
        "language_version": key[4],
    }


def load_yaml(text: str) -> object:
    """Parse YAML with PyYAML's safe loader.

    PyYAML is imported by name at run time: the hook environments that
    type-check this file do not carry it, and nothing else here needs it.
    """
    if len(text.encode()) > _MAX_CONFIG_BYTES:
        raise ValueError("the configuration is implausibly large")
    module = importlib.import_module("yaml")
    safe_load = cast("Callable[[str], object]", module.safe_load)
    return safe_load(text)


def _hook_options(hook: dict[str, object]) -> tuple[tuple[str, ...], str] | None:
    """Return a hook's dependencies and language version, or None."""
    raw = hook.get("additional_dependencies")
    dependencies = () if raw is None else values.strings(raw)
    version = hook.get("language_version", "")
    if dependencies is None or not isinstance(version, str):
        return None
    return dependencies, "" if version == "default" else version


def _local(hook: dict[str, object]) -> Key | str | None:
    """Return a local hook's key, a note, or None when nothing is needed."""
    language = values.string(hook.get("language"))
    options = _hook_options(hook)
    if language is None or options is None:
        return "a local hook has a malformed language or options"
    if language in NO_ENVIRONMENT:
        return None
    if language not in SEEDED:
        return f"language '{language}' is not one the bundle carries"
    return ("local", "", language, *options)


def _remote(url: str, rev: object, hook: dict[str, object]) -> Key | str:
    """Return a remote hook's key, or a note saying why it has none."""
    options = _hook_options(hook)
    language = hook.get("language", "")
    if options is None or not isinstance(language, str):
        return f"{url}: a hook has malformed options"
    if not url.startswith("https://"):
        return f"{url}: only https:// hook repositories are built"
    if not isinstance(rev, str) or not _COMMIT.fullmatch(rev):
        return f"{url}: rev is not a full commit id"
    return (url, rev, language, *options)


def requirements(config: object) -> tuple[list[tuple[Key, str]], list[str]]:
    """Return a configuration's (key, hook id) pairs, and notes."""
    found: list[tuple[Key, str]] = []
    notes: list[str] = []
    top = values.mapping(config)
    repos = values.sequence(top.get("repos")) if top else None
    if top is None or repos is None:
        return found, ["the configuration has no list of repos"]
    if top.get("default_language_version"):
        # It changes which toolchain a hook asks for, per language, and
        # a remote hook's language is not known until its repository is
        # cloned. No repository sets it today; refuse rather than guess.
        return found, ["default_language_version is not supported"]
    for entry in repos:
        repo = values.mapping(entry)
        url = values.string(repo.get("repo")) if repo else None
        hooks = values.sequence(repo.get("hooks")) if repo else None
        if repo is None or url is None or hooks is None:
            notes.append("a repos entry is malformed")
            continue
        if url in ("meta", "builtin"):
            continue
        for item in hooks:
            hook = values.mapping(item)
            name = values.string(hook.get("id")) if hook else None
            if hook is None or name is None:
                notes.append(f"{url}: a hook is malformed")
                continue
            key = (
                _local(hook) if url == "local" else _remote(url, repo.get("rev"), hook)
            )
            if isinstance(key, str):
                notes.append(key)
            elif key is not None:
                found.append((key, name))
    return found, notes


def survey(
    configs: Iterable[tuple[str, str]],
    settings: Settings,
    load: Callable[[str], object] = load_yaml,
) -> Plan:
    """Plan the units to build from (repository name, YAML text) pairs."""
    users: dict[Key, set[str]] = {}
    hooks: dict[Key, set[str]] = {}
    notes: set[str] = set()
    surveyed = 0
    for name, text in configs:
        surveyed += 1
        try:
            config = load(text)
        except Exception as error:  # noqa: BLE001 - any parser failure
            notes.add(f"{name}: unreadable configuration ({type(error).__name__})")
            continue
        found, problems = requirements(config)
        notes.update(f"{name}: {problem}" for problem in problems)
        for key, hook in found:
            if is_seed(key):
                continue
            users.setdefault(key, set()).add(name)
            hooks.setdefault(key, set()).add(hook)
    always = set(settings.always)
    units = [
        Unit(
            repo=key[0],
            rev=key[1],
            language=key[2],
            dependencies=key[3],
            language_version=key[4],
            hooks=tuple(sorted(hooks[key])),
            users=tuple(sorted(names)),
        )
        for key, names in users.items()
        if len(names) >= settings.min_repos or names & always
    ]
    units.sort(key=lambda unit: (-len(unit.users), unit.repo, unit.uid))
    return Plan(units=tuple(units), notes=tuple(sorted(notes)), surveyed=surveyed)
