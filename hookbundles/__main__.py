# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""Command line: python -m hookbundles <command>.

The build is four commands, each leaving a file the next one reads, so
a stage can be rerun or inspected on its own:

  fetch     the organisation's configurations  -> configs.jsonl
  plan      configurations                     -> plan.json
  build     plan, in the sandbox               -> <work>/
  assemble  what the sandbox produced          -> index.json, blobs/

and a lint job runs two:

  select    the blobs its configuration needs
  install   those blobs into an empty prek store
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import TYPE_CHECKING, cast

from hookbundles import assemble as assembly
from hookbundles import install as installer
from hookbundles import plan as planner
from hookbundles import sandbox, selection, settings, values

if TYPE_CHECKING:
    from collections.abc import Callable

# Exit status of 'install' when the bundle is sound but was built for
# a different runner, prek or store location. The caller carries on
# without it.
NOT_APPLICABLE = 3


class UsageError(Exception):
    """An input is not what the command expects."""


class Arguments:
    """The parsed command line, read by name and by type."""

    def __init__(self, namespace: argparse.Namespace) -> None:
        """Wrap what argparse parsed."""
        self._values: dict[str, object] = cast("dict[str, object]", vars(namespace))

    def path(self, name: str) -> Path:
        """Return a path option."""
        value = self._values.get(name)
        if not isinstance(value, Path):
            raise UsageError(f"--{name} is missing")
        return value

    def text(self, name: str) -> str:
        """Return a string option."""
        return values.string(self._values.get(name)) or ""

    def number(self, name: str) -> float:
        """Return a numeric option."""
        value = self._values.get(name)
        if isinstance(value, bool) or not isinstance(value, int | float):
            raise UsageError(f"--{name} is missing")
        return value


def _gh(*arguments: str) -> bytes | None:
    """Return the output of a gh api call, or None when it fails."""
    result = subprocess.run(  # noqa: S603
        ["gh", "api", *arguments],  # noqa: S607 - gh is found on PATH
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        check=False,
    )
    return result.stdout if result.returncode == 0 else None


def _fetch(arguments: Arguments) -> int:
    """Save every repository's default-branch configuration.

    Uses the REST API alone, which serves public repositories to the
    token of a workflow in any repository, a fork of this one included.
    """
    org = settings.load(arguments.path("config")).org
    listing = _gh(
        "--paginate",
        f"orgs/{org}/repos?per_page=100&type=public",
        "--jq",
        ".[] | select(.archived | not) | .name",
    )
    if listing is None:
        raise UsageError(f"gh could not list the repositories of {org}")
    names = sorted(listing.decode().split())

    def config_of(name: str) -> str | None:
        text = _gh(
            "--header",
            "Accept: application/vnd.github.raw",
            f"repos/{org}/{name}/contents/.pre-commit-config.yaml",
        )
        return None if text is None else text.decode(errors="replace")

    with ThreadPoolExecutor(max_workers=8) as pool:
        texts = list(pool.map(config_of, names))
    lines = [
        json.dumps({"name": name, "text": text})
        for name, text in zip(names, texts, strict=True)
    ]
    _ = arguments.path("out").write_text("\n".join(lines) + "\n")
    found = sum(text is not None for text in texts)
    print(f"Fetched {found} configurations from {len(names)} repositories")
    return 0


def _read_json(path: Path) -> dict[str, object]:
    data = values.mapping(values.parse(path.read_text()))
    if data is None:
        raise UsageError(f"{path} is not a JSON object")
    return data


def _read_configs(path: Path) -> list[tuple[str, str]]:
    """Read fetch's output: one {name, text} object a line."""
    configs: list[tuple[str, str]] = []
    for line in path.read_text().splitlines():
        entry = values.mapping(values.parse(line))
        name = values.string(entry.get("name")) if entry else None
        text = values.string(entry.get("text")) if entry else None
        if name is not None and text is not None:
            configs.append((name, text))
    return configs


def _plan(arguments: Arguments) -> int:
    """Work out which hook environments to build."""
    config = settings.load(arguments.path("config"))
    plan = planner.survey(_read_configs(arguments.path("configs")), config)
    _ = arguments.path("out").write_text(json.dumps(plan.to_json(), indent=2) + "\n")
    print(f"Surveyed {plan.surveyed} configurations: {len(plan.units)} units")
    for note in plan.notes:
        print(f"  note: {note}")
    return 0


def _build(arguments: Arguments) -> int:
    """Build every planned unit in the sandbox."""
    work = arguments.path("work")
    options = sandbox.Options(
        context=arguments.path("sandbox"),
        jobs=int(arguments.number("jobs")),
        timeout=arguments.number("timeout"),
    )
    record = sandbox.build(
        settings.load(arguments.path("config")),
        planner.Plan.from_json(_read_json(arguments.path("plan"))),
        work,
        options,
    )
    _ = (work / "build.json").write_text(json.dumps(record, indent=2))
    return 0


def _mib(entry: object, key: str) -> float:
    return (values.integer((values.mapping(entry) or {}).get(key)) or 0) / 2**20


def _summary(index: dict[str, object]) -> str:
    """Describe a bundle in Markdown, for a job summary."""
    tools = values.mapping(index.get("tools")) or {}
    clones = values.sequence(index.get("clones")) or []
    environments = values.sequence(index.get("environments")) or []
    skipped = values.sequence(index.get("skipped")) or []
    parts = (
        ("toolchains", list(tools.values())),
        ("clones", clones),
        ("environments", environments),
    )
    lines = [
        "### Hook bundle",
        "",
        "| Part | Blobs | Download MiB | Installed MiB |",
        "| --- | ---: | ---: | ---: |",
    ]
    for label, entries in parts:
        download = sum(_mib(entry, "size") for entry in entries)
        installed = sum(_mib(entry, "bytes") for entry in entries)
        lines.append(f"| {label} | {len(entries)} | {download:.0f} | {installed:.0f} |")
    lines += ["", f"{len(skipped)} units skipped."]
    for item in skipped:
        fields = values.mapping(item) or {}
        lines.append(f"- skipped `{fields.get('label')}`: {fields.get('reason')}")
    return "\n".join(lines) + "\n"


def _assemble(arguments: Arguments) -> int:
    """Check the sandbox's output, and write the index and the blobs."""
    config = settings.load(arguments.path("config"))
    work = arguments.path("work")
    out = arguments.path("out")
    writer = assembly.Writer(
        out / "blobs", config.prek_home, int(arguments.number("level"))
    )
    index = assembly.assemble(
        config,
        planner.Plan.from_json(_read_json(arguments.path("plan"))),
        _read_json(work / "build.json"),
        work,
        writer,
    )
    index["source"] = {
        "repository": os.environ.get("GITHUB_REPOSITORY", ""),
        "commit": os.environ.get("GITHUB_SHA", ""),
        "run": os.environ.get("GITHUB_RUN_ID", ""),
    }
    _ = (out / "index.json").write_text(json.dumps(index, indent=2) + "\n")
    print(_summary(index))
    return 0


def _select(arguments: Arguments) -> int:
    """Print the blobs a configuration's hooks need, one a line."""
    index = _read_json(arguments.path("index"))
    config = planner.load_yaml(arguments.path("hook_config").read_text())
    hooks = set(arguments.text("hooks").replace(",", " ").split()) or None
    chosen = selection.select(index, config, hooks)
    served = chosen.wanted - len(chosen.missing)
    summary = [
        f"The bundle has {chosen.environments} environments"
        + f" for {served} of {chosen.wanted} requirements",
        *(f"  not bundled: {label}" for label in chosen.missing),
    ]
    print("\n".join(summary), file=sys.stderr)
    print("\n".join(chosen.blobs))
    return 0


def _install(arguments: Arguments) -> int:
    """Install the blobs named on standard input into the prek store."""
    runner = installer.Runner(
        home=arguments.path("prek_home").absolute(),
        arch=arguments.text("arch"),
        image_os=arguments.text("image_os"),
        prek_version=arguments.text("prek_version"),
    )
    try:
        report = installer.install(
            _read_json(arguments.path("index")),
            arguments.path("blobs"),
            sys.stdin.read().split(),
            runner,
        )
    except installer.NotApplicable as error:
        print(f"Bundle not used: {error}", file=sys.stderr)
        return NOT_APPLICABLE
    except installer.InstallError as error:
        print(f"Bundle refused: {error}", file=sys.stderr)
        return 1
    print(json.dumps(report))
    return 0


_COMMANDS: dict[str, Callable[[Arguments], int]] = {
    "fetch": _fetch,
    "plan": _plan,
    "build": _build,
    "assemble": _assemble,
    "select": _select,
    "install": _install,
}

# Each command's options: flag, type and default. A default of None
# makes the option required.
_Option = tuple[str, type[Path] | type[str] | type[int] | type[float], object]
_CONFIG: _Option = ("--config", Path, Path("bundle.toml"))
_OPTIONS: dict[str, tuple[_Option, ...]] = {
    "fetch": (_CONFIG, ("--out", Path, None)),
    "plan": (_CONFIG, ("--configs", Path, None), ("--out", Path, None)),
    "build": (
        _CONFIG,
        ("--plan", Path, None),
        ("--work", Path, None),
        ("--sandbox", Path, Path("sandbox")),
        ("--jobs", int, min(os.cpu_count() or 2, 6)),
        ("--timeout", float, 900.0),
    ),
    "assemble": (
        _CONFIG,
        ("--plan", Path, None),
        ("--work", Path, None),
        ("--out", Path, None),
        ("--level", int, 19),
    ),
    "select": (
        ("--index", Path, None),
        ("--hook-config", Path, Path(".pre-commit-config.yaml")),
        ("--hooks", str, ""),
    ),
    "install": (
        ("--index", Path, None),
        ("--blobs", Path, None),
        (
            "--prek-home",
            Path,
            Path(os.environ.get("PREK_HOME") or Path.home() / ".cache" / "prek"),
        ),
        ("--arch", str, platform.machine()),
        ("--image-os", str, os.environ.get("ImageOS", "")),
        ("--prek-version", str, ""),
    ),
}


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="hookbundles",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    commands = parser.add_subparsers(dest="command", required=True)
    for name, run in _COMMANDS.items():
        command = commands.add_parser(name, help=(run.__doc__ or "").split("\n")[0])
        for flag, kind, default in _OPTIONS[name]:
            _ = command.add_argument(
                flag, type=kind, default=default, required=default is None
            )
    return parser


def main() -> int:
    """Run the command named on the command line."""
    arguments = Arguments(_parser().parse_args())
    try:
        return _COMMANDS[arguments.text("command")](arguments)
    except (
        UsageError,
        settings.SettingsError,
        sandbox.SandboxError,
        assembly.AssemblyError,
    ) as error:
        print(f"{type(error).__name__}: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
