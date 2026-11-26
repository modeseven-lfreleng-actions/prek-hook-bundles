# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""Shared fixtures: settings, and tar archives built member by member."""

from __future__ import annotations

import io
import json
import tarfile
from typing import TYPE_CHECKING, final

from hookbundles.plan import Unit
from hookbundles.settings import Settings

if TYPE_CHECKING:
    from pathlib import Path

HOME = "/home/runner/.cache/prek"
RUFF = "https://github.com/astral-sh/ruff-pre-commit"
REV = "9026c9127e153371d7a78dfcb0c45ae2046a3d10"

# Directory names shaped as prek shapes them.
SEED_DIR = "python-" + "s" * 20
ENV_DIR = "python-" + "a" * 20
OTHER_DIR = "python-" + "b" * 20


def python(home: str = HOME) -> str:
    """Return the path of the base stage's Python in a store at home."""
    return f"{home}/tools/python/cpython-3.13/bin/python"


def settings(home: str = HOME) -> Settings:
    """Return settings that survey nothing and build for home."""
    return Settings(
        org="example",
        min_repos=2,
        always=("template",),
        prek_home=home,
        image_os="ubuntu24",
        python="3.13",
        node="lts",
        golang="",
    )


def unit(
    repo: str = RUFF,
    rev: str = REV,
    dependencies: tuple[str, ...] = (),
    language: str = "",
) -> Unit:
    """Return a unit two repositories use."""
    return Unit(
        repo=repo,
        rev=rev,
        language=language,
        dependencies=dependencies,
        language_version="",
        hooks=("ruff",),
        users=("one", "two"),
    )


def marker(  # noqa: PLR0913 - each field is one a test varies
    directory: str,
    *,
    repo: str | None = RUFF,
    rev: str = REV,
    dependencies: tuple[str, ...] = (),
    language: str = "python",
    toolchain: str = "",
    home: str = HOME,
) -> bytes:
    """Return the marker prek would write for an environment."""
    data: dict[str, object] = {
        "schema_version": 1,
        "language": language,
        "language_version": "3.13.1",
        "dependencies": list(dependencies),
        "env_path": f"{home}/hooks/{directory}",
        "toolchain": toolchain or python(home),
        "extra": {},
    }
    if repo is not None:
        data["repo"] = {"url": repo, "rev": rev}
    return json.dumps(data).encode()


@final
class Tar:
    """Build a tar archive one member at a time."""

    def __init__(self, path: Path) -> None:
        """Open the archive at path for writing."""
        self.path = path
        self._tar = tarfile.open(path, "w")  # noqa: SIM115 - closed by close()

    def directory(self, name: str, mode: int = 0o755) -> Tar:
        """Add a directory."""
        info = tarfile.TarInfo(name)
        info.type = tarfile.DIRTYPE
        info.mode = mode
        self._tar.addfile(info)
        return self

    def file(self, name: str, data: bytes = b"x", mode: int = 0o644) -> Tar:
        """Add a regular file."""
        info = tarfile.TarInfo(name)
        info.size = len(data)
        info.mode = mode
        self._tar.addfile(info, io.BytesIO(data))
        return self

    def link(self, name: str, target: str, kind: bytes = tarfile.SYMTYPE) -> Tar:
        """Add a symbolic link, or another link type."""
        info = tarfile.TarInfo(name)
        info.type = kind
        info.linkname = target
        self._tar.addfile(info)
        return self

    def close(self) -> Path:
        """Finish the archive and return its path."""
        self._tar.close()
        return self.path


def environment(tar: Tar, directory: str, content: bytes, home: str = HOME) -> Tar:
    """Add a small environment with the given marker."""
    return (
        tar.directory(f"hooks/{directory}")
        .file(f"hooks/{directory}/.prek-hook.json", content)
        .directory(f"hooks/{directory}/bin")
        .file(f"hooks/{directory}/bin/tool", b"#!/bin/sh\n", 0o755)
        .link(f"hooks/{directory}/bin/python", python(home))
    )


def base(work: Path, home: str = HOME) -> None:
    """Write the three archives a base stage exports."""
    directory = work / "base"
    directory.mkdir(parents=True)
    _ = (
        Tar(directory / "tools.tar")
        .directory("tools")
        .directory("tools/python")
        .directory("tools/python/cpython-3.13")
        .directory("tools/python/cpython-3.13/bin")
        .file("tools/python/cpython-3.13/bin/python", b"elf", 0o755)
        .close()
    )
    repo = json.dumps({"repo": RUFF, "rev": REV}).encode()
    _ = (
        Tar(directory / "repos.tar")
        .directory("repos")
        .directory("repos/0123456789abcdef")
        .file("repos/0123456789abcdef/.prek-repo.json", repo)
        .file("repos/0123456789abcdef/.pre-commit-hooks.yaml", b"[]")
        .close()
    )
    seeds = Tar(directory / "hooks.tar").directory("hooks")
    content = marker(SEED_DIR, repo=None, home=home)
    _ = environment(seeds, SEED_DIR, content, home).close()
    (work / "units").mkdir()


def built(*units: Unit) -> dict[str, object]:
    """Return a build record in which every unit built."""
    return {
        "prek": "0.4.14",
        "arch": "x86_64",
        "units": {item.uid: {"status": "built"} for item in units},
    }
