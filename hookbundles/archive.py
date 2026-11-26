# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""The rules every archive member is held to, building and installing.

A bundle's blobs are tar archives of parts of a prek store: tools/,
repos/ and hooks/.
Parts of it come out of containers that ran a hook repository's own
install code, so an archive is read as hostile input and never
extracted on trust. The assembler copies members from one archive to
another without touching the file system, and the installer writes
only what these rules pass.

The rules, for a store rooted at the prek home directory:

  * a member is a regular file, a directory or a symbolic link, and
    nothing else: no hard link, device, FIFO or sparse member;
  * its name is relative, with no empty, '.' or '..' component;
  * a symbolic link resolves, lexically, to a path inside the store,
    and to wherever else in the store its group is allowed to reach;
  * no member's path passes through a symbolic link. Lexical checking
    of a link is only sound when the path leading to it is real.
"""

from __future__ import annotations

import posixpath
import tarfile
from dataclasses import dataclass, field
from pathlib import PurePosixPath
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Sequence

# The store directories a bundle may hold.
ROOTS = ("tools", "repos", "hooks")

# The store directory holding each language's toolchain. An environment
# is installed together with its language's toolchain: a Python or
# Node.js hook cannot start without it, and prek points a Go hook's
# GOROOT at it whether or not the compiled hook ever runs 'go'.
TOOL_BUCKETS = {"python": "python", "pygrep": "python", "node": "node", "golang": "go"}

# Permission bits kept on a member. Set-user-id, set-group-id and the
# sticky bit are dropped: nothing in a hook environment needs them.
_MODE = 0o777


class Rejected(Exception):
    """An archive member breaks one of the rules above."""


def clean_name(name: str) -> str:
    """Return a member name in canonical form, or raise Rejected."""
    if name.startswith("/") or "\0" in name or "\\" in name:
        raise Rejected(f"unacceptable member name {name!r}")
    trimmed = name.removeprefix("./").rstrip("/")
    parts = trimmed.split("/")
    if not trimmed or any(part in ("", ".", "..") for part in parts):
        raise Rejected(f"unacceptable member name {name!r}")
    return trimmed


def group_of(name: str) -> tuple[str, str] | None:
    """Return a clean name's (root, group), or None above group level.

    A group is what is accepted or dropped as a whole: one environment
    under hooks/, one clone under repos/, one toolchain under tools/.
    """
    parts = name.split("/", 2)
    if len(parts) < 2:
        return None
    return parts[0], parts[1]


def resolve_link(name: str, target: str, home: str) -> str:
    """Return where a symbolic link points, relative to the store.

    The resolution is lexical. Raise Rejected when the target leaves
    the store, or is absolute and not written in canonical form.
    """
    if not target or "\0" in target:
        raise Rejected(f"{name}: empty or malformed link target")
    if target.startswith("/"):
        if posixpath.normpath(target) != target:
            raise Rejected(f"{name}: link target {target!r} is not canonical")
        try:
            inside = PurePosixPath(target).relative_to(home)
        except ValueError:
            raise Rejected(
                f"{name}: link target {target!r} is outside the store"
            ) from None
        resolved = str(inside)
    else:
        joined = posixpath.join(posixpath.dirname(name), target)
        resolved = posixpath.normpath(joined)
    if resolved in (".", "..") or resolved.startswith(("../", "/")):
        raise Rejected(f"{name}: link target {target!r} leaves the store")
    return resolved


def within(path: str, prefixes: Sequence[str]) -> bool:
    """Whether a store-relative path is one of prefixes or beneath one."""
    return any(path == prefix or path.startswith(prefix + "/") for prefix in prefixes)


def reach(root: str, group: str) -> tuple[str, ...]:
    """Where a symbolic link in a group may point."""
    if root == "hooks":
        # Its own files, and the toolchain it was built against.
        return (f"hooks/{group}", "tools")
    if root == "repos":
        return (f"repos/{group}",)
    return ("tools",)


@dataclass
class Layout:
    """Reject any member whose path passes through a non-directory.

    Members arrive in archive order, so both orders are covered: a
    member under an earlier symbolic link, and a link replacing a
    directory that an earlier member was placed in. A name may repeat
    only as a directory; anything else would replace what was checked.
    """

    directories: set[str] = field(default_factory=set)
    others: set[str] = field(default_factory=set)
    parents: set[str] = field(default_factory=set)

    def add(self, name: str, *, directory: bool) -> None:
        """Record a member, or raise Rejected."""
        if name in self.others or (name in self.directories and not directory):
            raise Rejected(f"{name}: member name is repeated")
        ancestors: list[str] = []
        parent = posixpath.dirname(name)
        while parent:
            ancestors.append(parent)
            parent = posixpath.dirname(parent)
        if self.others.intersection(ancestors):
            raise Rejected(f"{name}: path passes through a non-directory")
        if not directory and name in self.parents:
            raise Rejected(f"{name}: member replaces a directory")
        self.parents.update(ancestors)
        (self.directories if directory else self.others).add(name)


def check_member(member: tarfile.TarInfo, home: str, reach: Sequence[str]) -> str:
    """Check one member, and return its clean name.

    reach lists the store-relative paths a symbolic link in this member
    may resolve to or beneath. Raise Rejected otherwise.
    """
    name = clean_name(member.name)
    if member.issym():
        resolved = resolve_link(name, member.linkname, home)
        if not within(resolved, reach):
            raise Rejected(f"{name}: link target {member.linkname!r} is out of reach")
    elif not (member.isreg() or member.isdir()):
        raise Rejected(f"{name}: member type {member.type!r} is not allowed")
    elif member.issparse():
        raise Rejected(f"{name}: sparse members are not allowed")
    return name


def sanitised(member: tarfile.TarInfo, name: str) -> tarfile.TarInfo:
    """Return a copy of a checked member carrying only what is kept.

    Ownership is dropped, so nothing in a bundle records who built it,
    and so are extended headers, which can override any field.
    """
    clean = tarfile.TarInfo(name)
    clean.mtime = int(member.mtime)
    if member.isdir():
        clean.type = tarfile.DIRTYPE
        # The owner must be able to enter and fill the directory.
        clean.mode = (member.mode & _MODE) | 0o700
    elif member.issym():
        clean.type = tarfile.SYMTYPE
        clean.linkname = member.linkname
        clean.mode = _MODE
    else:
        clean.type = tarfile.REGTYPE
        clean.size = member.size
        clean.mode = member.mode & _MODE
    return clean
