# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""Drive the sandbox: one trusted base stage, then one container a unit.

Building a hook environment runs whatever its repository's packaging
runs. Each unit therefore builds in a container of its own, started
from an image of the base stage's result and removed afterwards, so
one unit's install code never sees another unit's environment or the
toolchains the bundle will ship. The builder keeps a unit's hooks/
directory as a tar stream and nothing else; assemble.py decides what
of it is used. See docs/DESIGN.md, "Building".
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import TYPE_CHECKING

from hookbundles.archive import ROOTS

if TYPE_CHECKING:
    from pathlib import Path

    from hookbundles.plan import Plan, Unit
    from hookbundles.settings import Settings

IMAGE = "prek-hook-bundles-sandbox"

# The home directory of the image's user; see sandbox/Dockerfile.
_HOME = "/home/runner"

_BASE_TIMEOUT = 1800


class SandboxError(Exception):
    """Docker, or the trusted base stage, failed."""


@dataclass(frozen=True)
class Options:
    """How to run the sandbox.

    context is the directory holding the Dockerfile, jobs the number
    of units built at once, and timeout the seconds one unit may take.
    """

    context: Path
    jobs: int
    timeout: float


def _docker(
    arguments: list[str],
    *,
    stdin: bytes | None = None,
    stdout: int | None = None,
    timeout: float | None = None,
) -> subprocess.CompletedProcess[bytes]:
    """Run a docker command, returning its result whatever the status."""
    return subprocess.run(  # noqa: S603
        ["docker", *arguments],  # noqa: S607 - docker is found on PATH
        input=stdin,
        stdout=stdout,
        stderr=subprocess.STDOUT if stdout is not None else None,
        timeout=timeout,
        check=False,
    )


def _must(arguments: list[str], *, quiet: bool = False) -> None:
    result = _docker(arguments, stdout=subprocess.DEVNULL if quiet else None)
    if result.returncode != 0:
        raise SandboxError(f"docker {arguments[0]} failed")


def _log(message: str) -> None:
    _ = sys.stderr.write(message + "\n")
    _ = sys.stderr.flush()


def build_image(context: Path) -> str:
    """Build the sandbox image from sandbox/, and return its tag."""
    tag = f"{IMAGE}:tooling"
    _must(["build", "--quiet", "--tag", tag, str(context)], quiet=True)
    return tag


def seed_config(settings: Settings) -> dict[str, object]:
    """Return the configuration whose install seeds the toolchains.

    Its hooks are local and have no dependencies, so installing them
    downloads a toolchain and builds an empty environment: the one a
    repository's own dependency-free local hooks will use.
    """
    versions = {
        "python": settings.python,
        # pygrep takes no language_version. Its environment is a Python
        # one all the same, and prek gives it the Python the store
        # already manages: the release base.sh installed.
        "pygrep": "",
        "node": settings.node,
        "golang": settings.golang,
    }
    hooks: list[object] = []
    for language, version in versions.items():
        hook: dict[str, object] = {
            "id": f"seed-{language}",
            "name": f"seed-{language}",
            "language": language,
            "entry": "true",
        }
        if version:
            hook["language_version"] = version
        hooks.append(hook)
    return {"repos": [{"repo": "local", "hooks": hooks}]}


def _export(container: str, source: str, target: Path) -> bool:
    """Save a container directory as a tar archive, without extracting."""
    with target.open("wb") as handle:
        result = subprocess.run(  # noqa: S603
            ["docker", "cp", f"{container}:{source}", "-"],  # noqa: S607
            stdout=handle,
            check=False,
        )
    return result.returncode == 0


def run_base(
    image: str, settings: Settings, plan: Plan, work: Path
) -> tuple[dict[str, str], dict[str, bool]]:
    """Run the trusted base stage.

    Return what it reported about the platform, and which units'
    repositories it could clone. Leaves the result as the image
    '<IMAGE>:base' and the store's directories as archives under
    work/base/.
    """
    base = work / "base"
    units = base / "in" / "units"
    units.mkdir(parents=True, exist_ok=True)
    _ = (base / "in" / "seed.json").write_text(json.dumps(seed_config(settings)))
    for unit in plan.units:
        if not unit.local:
            _ = (units / f"{unit.uid}.json").write_text(json.dumps(unit.config()))

    name = f"phb-base-{os.getpid()}"
    _ = _docker(["rm", "--force", name], stdout=subprocess.DEVNULL)
    environment = [
        "--env",
        f"PREK_HOME={settings.prek_home}",
        "--env",
        f"PYTHON_VERSION={settings.python}",
    ]
    try:
        _must(["create", "--name", name, *environment, image, "base.sh"], quiet=True)
        _must(["cp", f"{base / 'in'}/.", f"{name}:{_HOME}/in"], quiet=True)
        result = subprocess.run(  # noqa: S603
            ["docker", "start", "--attach", name],  # noqa: S607
            stdout=subprocess.PIPE,
            timeout=_BASE_TIMEOUT,
            check=False,
        )
        if result.returncode != 0:
            raise SandboxError("the base stage failed")
        report: dict[str, str] = {}
        clones: dict[str, bool] = {}
        for line in result.stdout.decode(errors="replace").splitlines():
            fields = line.split()
            if len(fields) == 3 and fields[0] == "CLONE":
                clones[fields[1]] = fields[2] == "ok"
            elif len(fields) == 2 and fields[0] in ("PREK", "ARCH"):
                report[fields[0].lower()] = fields[1]
        if "prek" not in report or "arch" not in report:
            raise SandboxError("the base stage did not report its platform")
        _must(["commit", name, f"{IMAGE}:base"], quiet=True)
        for root in ROOTS:
            if not _export(name, f"{settings.prek_home}/{root}", base / f"{root}.tar"):
                raise SandboxError(f"could not export the base {root}/")
    finally:
        _ = _docker(["rm", "--force", name], stdout=subprocess.DEVNULL)
    return report, clones


def run_unit(
    unit: Unit, settings: Settings, work: Path, timeout: float
) -> dict[str, object]:
    """Build one unit in its own container; return its status.

    The container has no capabilities, cannot gain privileges, and
    starts from the base image, so it sees the toolchains and clones
    but nothing any other unit built.
    """
    name = f"phb-unit-{unit.uid}-{os.getpid()}"
    log = work / "units" / f"{unit.uid}.log"
    command = [
        "run",
        "--name",
        name,
        "--interactive",
        "--cap-drop",
        "ALL",
        "--security-opt",
        "no-new-privileges",
        "--pids-limit",
        "2048",
        "--env",
        f"PREK_HOME={settings.prek_home}",
        # uv hard-links from its cache by default, and a hard link
        # is a member type the bundle refuses.
        "--env",
        "UV_LINK_MODE=copy",
        f"{IMAGE}:base",
        "unit.sh",
    ]
    started = time.monotonic()
    status = "failed"
    try:
        with log.open("wb") as handle:
            result = _docker(
                command,
                stdin=json.dumps(unit.config()).encode(),
                stdout=handle.fileno(),
                timeout=timeout,
            )
        archive = work / "units" / f"{unit.uid}.tar"
        if result.returncode == 0 and _export(
            name, f"{settings.prek_home}/hooks", archive
        ):
            status = "built"
    except subprocess.TimeoutExpired:
        _ = _docker(["kill", name], stdout=subprocess.DEVNULL)
        status = "timed out"
    finally:
        _ = _docker(["rm", "--force", name], stdout=subprocess.DEVNULL)
    seconds = round(time.monotonic() - started, 1)
    _log(f"  {status:9s} {seconds:6.1f}s  {unit.label}")
    return {"status": status, "seconds": seconds}


def build(
    settings: Settings, plan: Plan, work: Path, options: Options
) -> dict[str, object]:
    """Run the whole sandbox stage, and return the build record."""
    (work / "units").mkdir(parents=True, exist_ok=True)
    _log("Building the sandbox image")
    image = build_image(options.context)
    _log("Running the base stage")
    started = time.monotonic()
    report, cloned = run_base(image, settings, plan, work)
    _log(f"Base stage done in {time.monotonic() - started:.1f}s")

    results: dict[str, object] = {}
    buildable: list[Unit] = []
    for unit in plan.units:
        if unit.local or cloned.get(unit.uid):
            buildable.append(unit)
        else:
            results[unit.uid] = {"status": "could not be cloned", "seconds": 0}
    _log(f"Building {len(buildable)} units, {options.jobs} at a time")

    def run(unit: Unit) -> dict[str, object]:
        return run_unit(unit, settings, work, options.timeout)

    with ThreadPoolExecutor(max_workers=options.jobs) as pool:
        for unit, outcome in zip(buildable, pool.map(run, buildable), strict=True):
            results[unit.uid] = outcome
    return {**report, "units": results}
