# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""Fetch, verify and install a published bundle: what a lint job does.

The order is the security argument, so it is fixed here:

  1. find the bundle for this runner and prek release, by tag;
  2. download the index and the publisher's attestation over it;
  3. verify the attestation, and stop unless it holds;
  4. only then read the index: check it fits, choose blobs;
  5. download each chosen blob, checking it against the digest the
     verified index gives;
  6. install.

Nothing from the registry is parsed for meaning before step 3 beyond
the manifest's list of layers, and a blob is only ever fetched by a
digest that the verified index names.
"""

from __future__ import annotations

import subprocess
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import TYPE_CHECKING

from hookbundles import install, selection, values
from hookbundles.registry import (
    ATTESTATION_TYPE,
    INDEX_TYPE,
    NotPublished,
    Reference,
    Registry,
    RegistryError,
    tag_for,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Collection
    from pathlib import Path

_MAX_INDEX_BYTES = 8 * 1024 * 1024

# Blobs fetched at once. A job's selection is a few dozen blobs, most
# of them small, and one at a time the fetch took longer than the
# install it feeds.
_DOWNLOADS = 8


class Unverified(Exception):
    """The index is not one the trusted publisher attested."""


@dataclass(frozen=True)
class Publisher:
    """Who a bundle must come from to be trusted.

    repository is the GitHub repository that builds bundles, workflow
    the path of the workflow that publishes them, and ref the git ref
    it must have run on. Together they are the identity in the signing
    certificate, so a bundle built by any other workflow, or by that
    workflow on any other branch, fails verification.
    """

    repository: str
    workflow: str
    ref: str

    @property
    def identity(self) -> str:
        """The certificate identity the attestation must carry."""
        return f"https://github.com/{self.repository}/{self.workflow}@{self.ref}"


def gh_verify(index: Path, attestation: Path, publisher: Publisher) -> None:
    """Verify the attestation over the index with the GitHub CLI.

    The attestation travels with the bundle, so the check needs no
    GitHub API call and no token: only Sigstore's trust root.
    """
    command = [
        "gh",
        "attestation",
        "verify",
        str(index),
        "--bundle",
        str(attestation),
        "--repo",
        publisher.repository,
        "--cert-identity",
        publisher.identity,
        "--deny-self-hosted-runners",
    ]
    try:
        result = subprocess.run(  # noqa: S603
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=120,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise Unverified(f"gh attestation verify did not run: {error}") from error
    if result.returncode != 0:
        detail = result.stdout.decode(errors="replace").strip().splitlines()
        raise Unverified(detail[-1] if detail else "gh attestation verify failed")


@dataclass(frozen=True)
class Request:
    """What a lint job asks for.

    repository is the bundle's registry path, 'host/name'. config is
    the job's parsed hook configuration and hooks the ids it will run,
    or None for all of them. password is a registry token, needed only
    while a bundle's package is private.
    """

    repository: str
    publisher: Publisher
    config: object
    hooks: Collection[str] | None
    work: Path
    password: str = ""


def pull(
    request: Request,
    runner: install.Runner,
    verify: Callable[[Path, Path, Publisher], None] = gh_verify,
) -> dict[str, object]:
    """Fetch, verify and install the bundle for a runner; return a report.

    Raise install.NotApplicable when no bundle serves the runner,
    Unverified when the index fails verification, and RegistryError or
    install.InstallError when the bundle cannot be fetched or written.
    """
    tag = tag_for(runner.image_os, runner.arch, runner.prek_version)
    registry = Registry(Reference.parse(request.repository, tag), request.password)
    try:
        layers = registry.layers()
    except NotPublished:
        raise install.NotApplicable(f"no bundle is published as {tag}") from None
    if INDEX_TYPE not in layers or ATTESTATION_TYPE not in layers:
        raise RegistryError("the bundle lacks an index or an attestation")

    request.work.mkdir(parents=True, exist_ok=True)
    index_path = request.work / "index.json"
    attestation = request.work / "attestation.jsonl"
    registry.download(layers[INDEX_TYPE], index_path, _MAX_INDEX_BYTES)
    registry.download(layers[ATTESTATION_TYPE], attestation, _MAX_INDEX_BYTES)
    verify(index_path, attestation, request.publisher)

    index = values.mapping(values.parse(index_path.read_bytes()))
    if index is None:
        raise RegistryError("the verified index is not a JSON object")
    install.check_fit(index, runner)
    chosen = selection.select(index, request.config, request.hooks)
    sizes = selection.sizes(index)
    blobs = request.work / "blobs"
    blobs.mkdir(exist_ok=True)

    def fetch(name: str) -> None:
        digest = "sha256:" + name.split(".", 1)[0]
        registry.download(digest, blobs / name, sizes.get(name, 0))

    # The registry client is read-only by now: layers() above did the
    # one login it will do. list() raises the first failure, if any.
    with ThreadPoolExecutor(max_workers=_DOWNLOADS) as pool:
        _ = list(pool.map(fetch, chosen.blobs))
    report = install.install(index, blobs, chosen.blobs, runner)
    return {
        **report,
        "tag": tag,
        "environments": chosen.environments,
        "wanted": chosen.wanted,
        "missing": list(chosen.missing),
        "download": sum(sizes.get(name, 0) for name in chosen.blobs),
    }
