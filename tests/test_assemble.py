# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""The assembler: what a unit's build may and may not put in a bundle.

Each refusal here is an attack the design has to stop: one unit's
install code trying to supply the environment, toolchain or clone
that some other hook's lint job would then run.
"""

from __future__ import annotations

import hashlib
import tarfile
import tempfile
import unittest
from pathlib import Path
from typing import TYPE_CHECKING, override

from hookbundles import assemble, values
from hookbundles.plan import Plan, Unit
from tests import fixtures
from tests.fixtures import ENV_DIR, HOME, OTHER_DIR, SEED_DIR, Tar, marker

if TYPE_CHECKING:
    from collections.abc import Callable

    Extend = Callable[[Tar], Tar]


def _file(name: str, data: bytes = b"x", mode: int = 0o644) -> Extend:
    """Return a step adding a file to a unit's archive."""

    def extend(tar: Tar) -> Tar:
        return tar.file(name, data, mode)

    return extend


def _link(name: str, target: str, kind: bytes = tarfile.SYMTYPE) -> Extend:
    """Return a step adding a link to a unit's archive."""

    def extend(tar: Tar) -> Tar:
        return tar.link(name, target, kind)

    return extend


class _Case(unittest.TestCase):
    """A work directory holding a base export, and helpers to assemble."""

    work: Path = Path()
    out: Path = Path()

    @override
    def setUp(self) -> None:
        """Create the work directory with a sound base export."""
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.work = Path(temporary.name) / "work"
        self.out = Path(temporary.name) / "out"
        fixtures.base(self.work)

    def archive(self, unit: Unit, content: bytes | None = None, *steps: Extend) -> None:
        """Write a unit's archive: one environment, then any extra steps."""
        tar = Tar(self.work / "units" / f"{unit.uid}.tar").directory("hooks")
        tar = fixtures.environment(tar, ENV_DIR, content or marker(ENV_DIR))
        for step in steps:
            tar = step(tar)
        _ = tar.close()

    def assemble(
        self, *units: Unit, build: dict[str, object] | None = None
    ) -> dict[str, object]:
        """Assemble, with every unit recorded as built unless build says."""
        return assemble.assemble(
            fixtures.settings(),
            Plan(units=units, notes=(), surveyed=0),
            fixtures.built(*units) if build is None else build,
            self.work,
            assemble.Writer(self.out / "blobs", HOME, 0),
        )

    def entries(self, index: dict[str, object], key: str) -> list[dict[str, object]]:
        """Return an index list as mappings."""
        items = values.sequence(index[key]) or []
        return [values.mapping(item) or {} for item in items]

    def reason(self, unit: Unit, content: bytes | None = None, *steps: Extend) -> str:
        """Assemble one unit that must be skipped; return the reason."""
        self.archive(unit, content, *steps)
        index = self.assemble(unit)
        built = [entry["directory"] for entry in self.entries(index, "environments")]
        self.assertEqual(built, [SEED_DIR])
        (skipped,) = self.entries(index, "skipped")
        return str(skipped["reason"])

    def blob(self, entry: dict[str, object]) -> Path:
        """Return the path of an index entry's blob."""
        return self.out / "blobs" / str(entry["blob"])


class AcceptTest(_Case):
    """What a sound build contributes."""

    def test_unit_environment_is_bundled(self) -> None:
        """An environment whose marker names the unit is accepted."""
        unit = fixtures.unit()
        self.archive(unit)
        index = self.assemble(unit)
        seed, built = self.entries(index, "environments")
        self.assertEqual((seed["unit"], built["unit"]), ("seed", unit.uid))
        self.assertEqual(built["directory"], ENV_DIR)
        self.assertEqual(
            built["requested"],
            {
                "repo": unit.repo,
                "rev": unit.rev,
                "language": "",
                "dependencies": [],
                "language_version": "",
            },
        )
        self.assertEqual(index["skipped"], [])

    def test_blobs_are_named_by_their_content(self) -> None:
        """A blob's name is the SHA-256 of its bytes."""
        index = self.assemble()
        tools = values.mapping(index["tools"]) or {}
        entries = [
            values.mapping(tools["python"]) or {},
            *self.entries(index, "clones"),
            *self.entries(index, "environments"),
        ]
        self.assertEqual(len(entries), 3)
        for entry in entries:
            digest = hashlib.sha256(self.blob(entry).read_bytes()).hexdigest()
            self.assertEqual(entry["blob"], digest + ".tar")

    def test_blob_holds_its_own_group_only(self) -> None:
        """An environment's blob has no member outside its directory."""
        unit = fixtures.unit()
        self.archive(unit)
        built = self.entries(self.assemble(unit), "environments")[1]
        with tarfile.open(self.blob(built)) as tar:
            names = tar.getnames()
        self.assertTrue(names)
        self.assertTrue(all(name.startswith(f"hooks/{ENV_DIR}") for name in names))

    def test_repository_default_dependencies_are_accepted(self) -> None:
        """A hook setting none takes its repository's own defaults."""
        unit = fixtures.unit()
        self.archive(unit, marker(ENV_DIR, dependencies=("./core[extra]",)))
        self.assertEqual(self.assemble(unit)["skipped"], [])

    def test_local_unit_is_accepted(self) -> None:
        """A local hook's environment names no repository."""
        unit = fixtures.unit("local", "", ("aislop@1",), "python")
        self.archive(unit, marker(ENV_DIR, repo=None, dependencies=("aislop@1",)))
        self.assertEqual(self.assemble(unit)["skipped"], [])

    def test_permission_bits_beyond_rwx_are_dropped(self) -> None:
        """Set-user-id does not survive into a blob."""
        unit = fixtures.unit()
        self.archive(unit, None, _file(f"hooks/{ENV_DIR}/bin/su", b"x", 0o4755))
        built = self.entries(self.assemble(unit), "environments")[1]
        with tarfile.open(self.blob(built)) as tar:
            self.assertEqual(tar.getmember(f"hooks/{ENV_DIR}/bin/su").mode, 0o755)

    def test_unfinished_directory_is_ignored(self) -> None:
        """A directory prek never marked is not an environment."""
        unit = fixtures.unit()
        self.archive(unit, None, _file(f"hooks/{OTHER_DIR}/partial"))
        index = self.assemble(unit)
        self.assertEqual(len(self.entries(index, "environments")), 2)
        self.assertEqual(index["skipped"], [])


class RefuseTest(_Case):
    """What a hostile build cannot contribute."""

    def test_marker_naming_another_repository(self) -> None:
        """A unit cannot supply the environment of a different hook."""
        content = marker(ENV_DIR, repo="https://github.com/pre-commit/mirrors-mypy")
        reason = self.reason(fixtures.unit(), content)
        self.assertIn("repository other than the unit's", reason)

    def test_marker_naming_another_revision(self) -> None:
        """Nor that of another revision of its own repository."""
        reason = self.reason(fixtures.unit(), marker(ENV_DIR, rev="0" * 40))
        self.assertIn("repository other than the unit's", reason)

    def test_remote_unit_cannot_pose_as_local(self) -> None:
        """Nor the environment every dependency-free local hook uses."""
        reason = self.reason(fixtures.unit(), marker(ENV_DIR, repo=None))
        self.assertIn("repository other than the unit's", reason)

    def test_marker_with_other_dependencies(self) -> None:
        """Explicit dependencies must be the unit's, in order."""
        unit = fixtures.unit(dependencies=("a", "b"))
        reason = self.reason(unit, marker(ENV_DIR, dependencies=("b", "a")))
        self.assertIn("dependencies are not the unit's", reason)

    def test_local_unit_with_other_language(self) -> None:
        """A local unit's environment is in the language it asked for."""
        unit = fixtures.unit("local", "", ("x",), "node")
        reason = self.reason(unit, marker(ENV_DIR, repo=None, dependencies=("x",)))
        self.assertIn("language is not the unit's", reason)

    def test_toolchain_outside_the_base(self) -> None:
        """An environment may not point prek at a toolchain of its own."""
        for toolchain in (
            "/usr/bin/python3",
            f"{HOME}/tools/python/planted/bin/python",
            f"{HOME}/tools/python/cpython-3.13/../../../x",
            f"{HOME}/hooks/{ENV_DIR}/bin/python",
        ):
            with self.subTest(toolchain=toolchain):
                content = marker(ENV_DIR, toolchain=toolchain)
                self.assertIn("toolchain", self.reason(fixtures.unit(), content))

    def test_marker_lying_about_its_directory(self) -> None:
        """env_path must be where the environment is."""
        reason = self.reason(fixtures.unit(), marker(OTHER_DIR))
        self.assertIn("env_path", reason)

    def test_symbolic_link_out_of_reach(self) -> None:
        """A link may reach its own environment and tools/, no further."""
        for target in (
            "/etc/passwd",
            "../../../../etc/passwd",
            f"../../{SEED_DIR}/bin/tool",
            f"{HOME}/hooks/{SEED_DIR}/bin/tool",
            f"{HOME}/repos/0123456789abcdef/x",
            f"{HOME}/tools/../../x",
        ):
            with self.subTest(target=target):
                step = _link(f"hooks/{ENV_DIR}/bin/escape", target)
                reason = self.reason(fixtures.unit(), None, step)
                self.assertIn("link target", reason)

    def test_member_types_other_than_file_directory_link(self) -> None:
        """Hard links and devices are refused outright."""
        for kind in (tarfile.LNKTYPE, tarfile.CHRTYPE, tarfile.FIFOTYPE):
            with self.subTest(kind=kind):
                step = _link(f"hooks/{ENV_DIR}/odd", f"hooks/{ENV_DIR}/bin/tool", kind)
                reason = self.reason(fixtures.unit(), None, step)
                self.assertIn("not allowed", reason)

    def test_member_placed_through_a_symbolic_link(self) -> None:
        """A path through a link is refused, whichever comes first."""
        reason = self.reason(
            fixtures.unit(),
            None,
            _link(f"hooks/{ENV_DIR}/alias", "bin"),
            _file(f"hooks/{ENV_DIR}/alias/planted"),
        )
        self.assertIn("passes through a non-directory", reason)
        reason = self.reason(
            fixtures.unit(),
            None,
            _file(f"hooks/{ENV_DIR}/lib/module"),
            _link(f"hooks/{ENV_DIR}/lib", "bin"),
        )
        self.assertIn("replaces a directory", reason)

    def test_member_outside_hooks(self) -> None:
        """A unit's archive may hold hooks/ and nothing beside it."""
        for name in ("tools/python/cpython-3.13/bin/python", "../escape", "/abs"):
            with self.subTest(name=name):
                reason = self.reason(fixtures.unit(), None, _file(name))
                self.assertRegex(reason, "outside hooks/|unacceptable member name")

    def test_unit_cannot_replace_a_seed(self) -> None:
        """The base stage's environments come from the base stage."""
        unit = fixtures.unit()
        self.archive(
            unit,
            None,
            _file(f"hooks/{SEED_DIR}/.prek-hook.json", marker(SEED_DIR, repo=None)),
            _file(f"hooks/{SEED_DIR}/bin/tool", b"planted", 0o755),
        )
        seed = self.entries(self.assemble(unit), "environments")[0]
        with tarfile.open(self.blob(seed)) as tar:
            handle = tar.extractfile(f"hooks/{SEED_DIR}/bin/tool")
            self.assertEqual(handle.read() if handle else b"", b"#!/bin/sh\n")

    def test_second_unit_cannot_take_a_directory_name(self) -> None:
        """Two units claiming one directory: the later one is dropped."""
        first = fixtures.unit()
        second = fixtures.unit(dependencies=("x",))
        self.archive(first)
        self.archive(second, marker(ENV_DIR, dependencies=("x",)))
        (skipped,) = self.entries(self.assemble(first, second), "skipped")
        self.assertEqual(skipped["unit"], second.uid)

    def test_failed_build_is_skipped_with_its_status(self) -> None:
        """A unit that did not build is reported, not fatal."""
        unit = fixtures.unit()
        build: dict[str, object] = {"units": {unit.uid: {"status": "timed out"}}}
        (skipped,) = self.entries(self.assemble(unit, build=build), "skipped")
        self.assertEqual(skipped["reason"], "build timed out")

    def test_unsound_base_is_fatal(self) -> None:
        """A base export breaking a rule stops the build."""
        _ = (
            Tar(self.work / "base" / "tools.tar")
            .directory("tools")
            .directory("tools/python")
            .link("tools/python/escape", "/etc")
            .close()
        )
        with self.assertRaises(assemble.AssemblyError):
            _ = self.assemble()


if __name__ == "__main__":
    _ = unittest.main()
