# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""The lint job's half: choosing blobs, and installing them."""

from __future__ import annotations

import hashlib
import tempfile
import unittest
from pathlib import Path
from typing import override

from hookbundles import MANIFEST_NAME, assemble, install, selection, values
from hookbundles.plan import Plan
from tests import fixtures
from tests.fixtures import ENV_DIR, REV, RUFF, SEED_DIR, Tar, marker

# A directory name no blob of the fixture bundle holds.
_PLANTED = ("hooks", "python-" + "e" * 20)


class _Case(unittest.TestCase):
    """A bundle assembled for a store inside a temporary directory."""

    home: Path = Path()
    out: Path = Path()
    blobs: Path = Path()
    index: dict[str, object] = {}  # noqa: RUF012 - replaced in setUp
    names: tuple[str, ...] = ()

    @override
    def setUp(self) -> None:
        """Assemble a bundle of one seed and one ruff environment."""
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name).resolve()
        self.home = root / "store"
        self.out = root / "out"
        self.blobs = self.out / "blobs"
        home = str(self.home)
        work = root / "work"
        fixtures.base(work, home)
        unit = fixtures.unit()
        tar = Tar(work / "units" / f"{unit.uid}.tar").directory("hooks")
        content = marker(ENV_DIR, home=home)
        _ = fixtures.environment(tar, ENV_DIR, content, home).close()
        self.index = assemble.assemble(
            fixtures.settings(home),
            Plan(units=(unit,), notes=(), surveyed=0),
            fixtures.built(unit),
            work,
            assemble.Writer(self.blobs, home, 0),
        )
        self.names = selection.everything(self.index)

    def runner(self, **changes: str) -> install.Runner:
        """Return a runner the bundle fits, but for the changes given."""
        fit = {"arch": "x86_64", "image_os": "ubuntu24", "prek_version": ""}
        fit.update(changes)
        home = Path(fit.pop("home", str(self.home)))
        return install.Runner(home=home, **fit)

    def install(self, *extra: str) -> dict[str, object]:
        """Install every blob, and any extra ones, on a fitting runner."""
        names = (*self.names, *extra)
        return install.install(self.index, self.blobs, names, self.runner())

    def planted(self, tar: Tar) -> str:
        """Add a hostile blob to the bundle, and an index entry for it."""
        path = tar.close()
        name = hashlib.sha256(path.read_bytes()).hexdigest() + ".tar"
        _ = path.replace(self.blobs / name)
        entries = values.sequence(self.index["environments"]) or []
        entries.append({"blob": name, "directory": _PLANTED[1]})
        return name

    def hostile(self) -> Tar:
        """Start a hostile blob holding the directory the index gives it."""
        return Tar(self.out / "hostile.tar").directory("/".join(_PLANTED))

    def assert_nothing_written(self) -> None:
        """Require the store to hold no file at all."""
        files = [path for path in self.home.rglob("*") if not path.is_dir()]
        self.assertEqual(files, [])


class SelectTest(_Case):
    """Choosing blobs from a configuration."""

    def select(
        self, repo: dict[str, object], hooks: set[str] | None = None
    ) -> selection.Selection:
        """Select for a configuration of one repos entry."""
        return selection.select(self.index, {"repos": [repo]}, hooks)

    def seed(self) -> set[str]:
        """Return the seed environment's blob."""
        entries = values.sequence(self.index["environments"]) or []
        return {str((values.mapping(entries[0]) or {})["blob"])}

    def test_hook_brings_its_environment_toolchain_and_clone(self) -> None:
        """One remote hook needs three blobs."""
        chosen = self.select({"repo": RUFF, "rev": REV, "hooks": [{"id": "ruff"}]})
        self.assertEqual(set(chosen.blobs), set(self.names) - self.seed())
        self.assertEqual((chosen.environments, chosen.wanted), (1, 1))
        self.assertEqual(chosen.missing, ())

    def test_dependency_free_local_hook_uses_the_seed(self) -> None:
        """A local hook with no dependencies is served by the seed."""
        hook = {"id": "x", "language": "python"}
        chosen = self.select({"repo": "local", "hooks": [hook]})
        self.assertEqual((chosen.environments, chosen.missing), (1, ()))
        self.assertLessEqual(self.seed(), set(chosen.blobs))

    def test_other_revision_is_missing(self) -> None:
        """A hook at a revision the bundle lacks is reported, not served."""
        chosen = self.select({"repo": RUFF, "rev": "0" * 40, "hooks": [{"id": "ruff"}]})
        self.assertEqual((chosen.blobs, chosen.environments), ((), 0))
        self.assertEqual(chosen.missing, (RUFF,))

    def test_hook_filter(self) -> None:
        """A job running some hooks gets the blobs of those hooks."""
        entry: dict[str, object] = {
            "repo": RUFF,
            "rev": REV,
            "hooks": [{"id": "ruff"}],
        }
        self.assertEqual(self.select(entry, {"mypy"}).blobs, ())
        self.assertTrue(self.select(entry, {"ruff"}).blobs)


class InstallTest(_Case):
    """Writing blobs into an empty store."""

    def test_installs_what_was_built(self) -> None:
        """Files, modes, links and the index copy all arrive."""
        report = self.install()
        tool = self.home / "hooks" / ENV_DIR / "bin" / "tool"
        self.assertEqual(tool.read_bytes(), b"#!/bin/sh\n")
        self.assertEqual(tool.stat().st_mode & 0o777, 0o755)
        link = self.home / "hooks" / ENV_DIR / "bin" / "python"
        self.assertEqual(link.readlink(), Path(fixtures.python(str(self.home))))
        self.assertTrue(link.exists())
        self.assertTrue((self.home / "hooks" / SEED_DIR).is_dir())
        copy = values.parse((self.home / MANIFEST_NAME).read_text())
        self.assertEqual(copy, self.index)
        self.assertEqual(report["blobs"], len(self.names))

    def test_bundle_for_another_runner_is_not_applicable(self) -> None:
        """A sound bundle that does not fit is declined, writing nothing."""
        cases: tuple[dict[str, str], ...] = (
            {"arch": "aarch64"},
            {"image_os": "ubuntu22"},
            {"image_os": ""},
            {"prek_version": "0.4.15"},
            {"home": str(self.home) + "-elsewhere"},
        )
        for changes in cases:
            runner = self.runner(**changes)
            with (
                self.subTest(changes=changes),
                self.assertRaises(install.NotApplicable),
            ):
                _ = install.install(self.index, self.blobs, self.names, runner)
        self.assert_nothing_written()

    def test_populated_store_is_not_applicable(self) -> None:
        """The installer never merges into a store it did not create."""
        (self.home / "hooks" / "python-existing").mkdir(parents=True)
        with self.assertRaises(install.NotApplicable):
            _ = self.install()

    def test_blob_the_index_does_not_name(self) -> None:
        """A blob the index does not name is not installed."""
        with self.assertRaisesRegex(install.InstallError, "no such blob"):
            _ = self.install("0" * 64 + ".tar")
        self.assert_nothing_written()

    def test_altered_blob(self) -> None:
        """A blob must hash to its name."""
        with (self.blobs / self.names[0]).open("ab") as handle:
            _ = handle.write(b"\0" * 512)
        with self.assertRaisesRegex(install.InstallError, "does not match"):
            _ = self.install()
        self.assert_nothing_written()

    def test_blob_writing_outside_its_group(self) -> None:
        """A blob may write the one group the index gives it."""
        for member in (f"hooks/{ENV_DIR}/bin/planted", "tools/python/planted"):
            with self.subTest(member=member):
                name = self.planted(self.hostile().file(member))
                with self.assertRaisesRegex(install.InstallError, "outside"):
                    _ = self.install(name)
                self.assert_nothing_written()

    def test_blob_with_an_escaping_link(self) -> None:
        """The installer applies the link rule itself."""
        link = "/".join((*_PLANTED, "escape"))
        name = self.planted(self.hostile().link(link, "/etc/passwd"))
        with self.assertRaisesRegex(install.InstallError, "link target"):
            _ = self.install(name)
        self.assert_nothing_written()


if __name__ == "__main__":
    _ = unittest.main()
