# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""The planner: which requirements become units, and which do not."""

from __future__ import annotations

import json
import unittest

from hookbundles import plan
from tests.fixtures import REV, RUFF, settings


def _config(*repos: dict[str, object]) -> str:
    return json.dumps({"repos": list(repos)})


def _remote(url: str = RUFF, rev: object = REV, **hook: object) -> dict[str, object]:
    return {"repo": url, "rev": rev, "hooks": [{"id": "ruff", **hook}]}


def _local(language: str, **hook: object) -> dict[str, object]:
    entry = {"id": "x", "name": "x", "entry": "x", "language": language, **hook}
    return {"repo": "local", "hooks": [entry]}


def _survey(*configs: tuple[str, str]) -> plan.Plan:
    # JSON is YAML, so the tests need no YAML parser.
    return plan.survey(configs, settings(), json.loads)


class SurveyTest(unittest.TestCase):
    """Selection by how many repositories share a requirement."""

    def test_shared_requirement_becomes_one_unit(self) -> None:
        """Two repositories with the same hook share one unit."""
        found = _survey(("one", _config(_remote())), ("two", _config(_remote())))
        self.assertEqual(len(found.units), 1)
        self.assertEqual(found.units[0].users, ("one", "two"))
        self.assertEqual(found.surveyed, 2)

    def test_single_user_is_left_out(self) -> None:
        """A requirement one repository has is below the threshold."""
        found = _survey(("one", _config(_remote())), ("two", _config()))
        self.assertEqual(found.units, ())

    def test_always_repository_is_built_alone(self) -> None:
        """The canonical repository's requirements skip the threshold."""
        found = _survey(("template", _config(_remote())))
        self.assertEqual(len(found.units), 1)

    def test_dependencies_and_order_split_units(self) -> None:
        """prek compares dependencies in order, so the planner does too."""
        first = _remote(additional_dependencies=["a", "b"])
        second = _remote(additional_dependencies=["b", "a"])
        found = _survey(("template", _config(first, second, _remote())))
        self.assertEqual(len(found.units), 3)

    def test_url_spelling_splits_units(self) -> None:
        """prek keys an environment on the URL exactly as written."""
        found = _survey(("template", _config(_remote(), _remote(RUFF + ".git"))))
        self.assertEqual(len(found.units), 2)

    def test_hook_ids_are_collected(self) -> None:
        """Every hook id using the environment is in the unit."""
        entry: dict[str, object] = {
            "repo": RUFF,
            "rev": REV,
            "hooks": [{"id": "ruff"}, {"id": "ruff-format"}],
        }
        found = _survey(("template", _config(entry)))
        self.assertEqual(found.units[0].hooks, ("ruff", "ruff-format"))


class RefusalTest(unittest.TestCase):
    """What the planner will not build, and says so."""

    def _only_note(self, *repos: dict[str, object]) -> str:
        found = _survey(("template", _config(*repos)))
        self.assertEqual(found.units, ())
        self.assertEqual(len(found.notes), 1)
        return found.notes[0]

    def test_tag_rev_is_refused(self) -> None:
        """A tag can move; only a full commit id is built."""
        self.assertIn("full commit id", self._only_note(_remote(rev="v1.2.3")))

    def test_short_commit_is_refused(self) -> None:
        """An abbreviated commit id is not a full one."""
        self.assertIn("full commit id", self._only_note(_remote(rev=REV[:12])))

    def test_non_https_repository_is_refused(self) -> None:
        """Only https:// hook repositories are cloned."""
        note = self._only_note(_remote("git@github.com:astral-sh/ruff.git"))
        self.assertIn("https://", note)

    def test_unseeded_language_is_noted(self) -> None:
        """A language the base stage does not seed is not a unit."""
        note = self._only_note(_local("ruby", additional_dependencies=["x"]))
        self.assertIn("ruby", note)

    def test_default_language_version_is_refused(self) -> None:
        """It changes requirements in a way the planner cannot see."""
        text = json.dumps(
            {"default_language_version": {"python": "3.11"}, "repos": [_remote()]}
        )
        found = _survey(("template", text))
        self.assertEqual(found.units, ())
        self.assertIn("default_language_version", found.notes[0])

    def test_unreadable_configuration_is_noted(self) -> None:
        """One bad file does not stop the survey."""
        found = _survey(("bad", "{"), ("template", _config(_remote())))
        self.assertEqual(len(found.units), 1)
        self.assertIn("bad: unreadable", found.notes[0])

    def test_environmentless_and_seed_hooks_are_silent(self) -> None:
        """System hooks need nothing, and seeds are in every bundle."""
        found = _survey(
            ("template", _config(_local("system"), _local("python"), _local("pygrep")))
        )
        self.assertEqual((found.units, found.notes), ((), ()))


class UnitTest(unittest.TestCase):
    """What a unit tells prek to build."""

    def test_remote_config_keeps_only_what_shapes_the_environment(self) -> None:
        """Arguments and file filters do not reach the unit's build."""
        entry = _remote(args=["--fix"], additional_dependencies=["a"])
        found = _survey(("template", _config(entry))).units[0]
        expected = {
            "repos": [
                {
                    "repo": RUFF,
                    "rev": REV,
                    "hooks": [{"id": "ruff", "additional_dependencies": ["a"]}],
                }
            ]
        }
        self.assertEqual(found.config(), expected)

    def test_local_config_never_runs_the_repository_entry(self) -> None:
        """A local unit's entry is a placeholder, not the hook's own."""
        entry = _local("node", entry="rm -rf /", additional_dependencies=["a@1"])
        config = _survey(("template", _config(entry))).units[0].config()
        self.assertNotIn("rm -rf", json.dumps(config))

    def test_round_trip(self) -> None:
        """A plan survives being written and read back."""
        found = _survey(("template", _config(_remote(additional_dependencies=["a"]))))
        self.assertEqual(plan.Plan.from_json(found.to_json()), found)

    def test_uid_is_stable_and_distinct(self) -> None:
        """The identifier depends on the requirement and nothing else."""
        entries = (_remote(), _remote(additional_dependencies=["a"]))
        first, second = _survey(("template", _config(*entries))).units
        again = _survey(("other", _config(_remote())), ("more", _config(_remote())))
        self.assertNotEqual(first.uid, second.uid)
        self.assertIn(again.units[0].uid, (first.uid, second.uid))


if __name__ == "__main__":
    _ = unittest.main()
