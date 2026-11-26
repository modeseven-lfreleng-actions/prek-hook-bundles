# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""Install blobs from a bundle into an empty prek store.

This is the half a lint job runs, so it needs the standard library and
nothing else. It holds every blob to the rules the assembler applied
when it wrote it (see archive.py), and to the index: a blob must hash
to the name the index gives it, and may only write inside the one
toolchain, clone or environment the index says it holds.

It does not establish who made the bundle. The index is the root of
trust for the blobs, and verifying the publisher's attestation over
the index is the caller's job, done before this runs.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import shutil
import subprocess
import tarfile
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING

from hookbundles import MANIFEST_NAME, SCHEMA, values
from hookbundles.archive import (
    ROOTS,
    Layout,
    Rejected,
    check_member,
    clean_name,
    group_of,
    reach,
)

if TYPE_CHECKING:
    from collections.abc import Generator, Sequence
    from pathlib import Path

_CHUNK = 1024 * 1024


class NotApplicable(Exception):
    """The bundle is sound but does not fit this runner; build as usual."""


class InstallError(Exception):
    """A blob is missing or malformed, or could not be written."""


@dataclass(frozen=True)
class Runner:
    """Where a bundle is being installed, and what will use it.

    An empty prek_version skips that comparison, for a caller that has
    not yet decided which prek it will run.
    """

    home: Path
    arch: str
    image_os: str
    prek_version: str = ""


def check_fit(index: dict[str, object], runner: Runner) -> None:
    """Raise NotApplicable unless the bundle was built for this runner."""
    prek = values.mapping(index.get("prek")) or {}
    platform = values.mapping(index.get("platform")) or {}
    if index.get("schema") != SCHEMA:
        raise NotApplicable("the bundle layout is not one this installer knows")
    checks = (
        ("prek home", prek.get("home"), str(runner.home)),
        ("architecture", platform.get("arch"), runner.arch),
        ("runner image", platform.get("image_os"), runner.image_os),
    )
    for label, built_for, here in checks:
        if built_for != here:
            raise NotApplicable(f"{label}: built for {built_for!r}, here {here!r}")
    if runner.prek_version and prek.get("version") != runner.prek_version:
        raise NotApplicable(
            f"prek: built for {prek.get('version')!r}, here {runner.prek_version!r}"
        )


def groups(index: dict[str, object]) -> dict[str, tuple[str, str]]:
    """Map each blob the index names to the one group it may write."""
    found: dict[str, tuple[str, str]] = {}
    tools = values.mapping(index.get("tools")) or {}
    for bucket, item in tools.items():
        name = values.string((values.mapping(item) or {}).get("blob"))
        if name:
            found[name] = ("tools", bucket)
    for key, root in (("clones", "repos"), ("environments", "hooks")):
        for item in values.sequence(index.get(key)) or []:
            entry = values.mapping(item) or {}
            name = values.string(entry.get("blob"))
            directory = values.string(entry.get("directory"))
            if name and directory:
                found[name] = (root, directory)
    return found


def _require_empty(home: Path) -> None:
    """Refuse a store that already holds anything a bundle would."""
    for root in ROOTS:
        path = home / root
        if path.is_symlink() or (path.exists() and any(path.iterdir())):
            raise NotApplicable(f"the store already has content in {root}/")


def _verify(blob: Path) -> None:
    """Require a blob's content to hash to the name the index gave it."""
    digest = hashlib.sha256()
    try:
        with blob.open("rb") as handle:
            while chunk := handle.read(_CHUNK):
                digest.update(chunk)
    except OSError as error:
        raise InstallError(f"{blob.name}: {error.strerror}") from error
    if digest.hexdigest() != blob.name.split(".", 1)[0]:
        raise InstallError(f"{blob.name}: content does not match its name")


@contextlib.contextmanager
def _open(blob: Path) -> Generator[tarfile.TarFile]:
    """Open a blob as a tar stream, decompressing a .zst on the way."""
    if blob.suffix != ".zst":
        with tarfile.open(blob, "r|") as tar:
            yield tar
        return
    command = ["zstd", "--decompress", "--stdout", "--quiet", "--", str(blob)]
    with subprocess.Popen(command, stdout=subprocess.PIPE) as process:  # noqa: S603
        stream = process.stdout
        if stream is None:
            raise InstallError("zstd gave no output stream")
        try:
            with tarfile.open(fileobj=stream, mode="r|") as tar:
                yield tar
        except BaseException:
            process.kill()
            raise
        # tarfile stops at the end-of-archive marker; let zstd finish
        # writing the padding after it, so its exit status means something.
        while stream.read(_CHUNK):
            pass
        if process.wait() != 0:
            raise InstallError(f"{blob.name}: zstd could not decompress it")


def _write(tar: tarfile.TarFile, home: Path, group: tuple[str, str]) -> tuple[int, int]:
    """Write a blob's members under home; return files and bytes.

    Every member must lie in the group the index assigned the blob.
    """
    layout = Layout()
    directories: list[tuple[Path, tarfile.TarInfo]] = []
    files = size = 0
    for member in tar:
        name = clean_name(member.name)
        if group_of(name) != group:
            raise Rejected(f"{name}: member is outside {'/'.join(group)}/")
        _ = check_member(member, str(home), reach(*group))
        layout.add(name, directory=member.isdir())
        target = home / name
        if member.isdir():
            target.mkdir()
            directories.append((target, member))
        elif member.issym():
            os.symlink(member.linkname, target)
        else:
            flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW
            source = tar.extractfile(member)
            with os.fdopen(os.open(target, flags, member.mode & 0o777), "wb") as out:
                if source is not None:
                    shutil.copyfileobj(source, out, _CHUNK)
            # A Python source file's time is recorded in its compiled
            # twin; a changed time would recompile every module.
            os.utime(target, (member.mtime, member.mtime))
            files += 1
            size += member.size
    # Last, and deepest first, so a read-only directory is filled
    # before it is closed and its time is not disturbed by its children.
    for target, member in reversed(directories):
        target.chmod((member.mode & 0o777) | 0o700)
        os.utime(target, (member.mtime, member.mtime))
    return files, size


def install(
    index: dict[str, object],
    blobs: Path,
    names: Sequence[str],
    runner: Runner,
) -> dict[str, object]:
    """Install the named blobs into the runner's store; return a report.

    Raise NotApplicable, having written nothing, when the bundle does
    not fit; InstallError, having removed what was written, when a
    blob breaks a rule.
    """
    started = time.monotonic()
    home = runner.home
    check_fit(index, runner)
    _require_empty(home)
    allowed = groups(index)
    for name in names:
        if name not in allowed:
            raise InstallError(f"{name}: the index names no such blob")
        _verify(blobs / name)
    files = size = 0
    try:
        for root in ROOTS:
            (home / root).mkdir(parents=True, exist_ok=True)
        for name in names:
            with _open(blobs / name) as tar:
                written = _write(tar, home, allowed[name])
            files += written[0]
            size += written[1]
    except (Rejected, OSError, tarfile.TarError, InstallError) as error:
        for root in ROOTS:
            shutil.rmtree(home / root, ignore_errors=True)
        raise InstallError(str(error)) from error
    _ = (home / MANIFEST_NAME).write_text(json.dumps(index, indent=2) + "\n")
    return {
        "blobs": len(names),
        "files": files,
        "bytes": size,
        "seconds": round(time.monotonic() - started, 2),
    }
