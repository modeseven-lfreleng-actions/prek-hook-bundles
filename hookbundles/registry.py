# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""Read a published bundle from an OCI registry.

A lint job runs this, so it uses the standard library alone: no ORAS
and no Docker. It needs three things of a registry, all part of the
OCI distribution API: a token for anonymous pulls, a manifest by tag,
and a blob by digest.

The registry is transport and nothing more. What it serves is checked
elsewhere, never believed: the index against the publisher's
attestation (verify.py), and each blob against the SHA-256 that names
it, here as it downloads and again when it is installed.
"""

from __future__ import annotations

import base64
import hashlib
import http.client
import re
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import TYPE_CHECKING, cast, final

from hookbundles import values

if TYPE_CHECKING:
    from http.client import HTTPResponse
    from pathlib import Path

# What the publish job gives each file it pushes.
ARTIFACT_TYPE = "application/vnd.lfreleng.prek-hook-bundle.v1"
INDEX_TYPE = "application/vnd.lfreleng.prek-hook-bundle.index.v1+json"
ATTESTATION_TYPE = "application/vnd.dev.sigstore.bundle.v0.3+json"
BLOB_TYPE = "application/vnd.lfreleng.prek-hook-bundle.blob.v1.tar+zstd"

_MANIFEST_TYPE = "application/vnd.oci.image.manifest.v1+json"
_NAME = re.compile(r"[a-z0-9]+(?:[._-][a-z0-9]+)*(?:/[a-z0-9]+(?:[._-][a-z0-9]+)*)*")
_HOST = re.compile(r"[A-Za-z0-9.-]+(?::[0-9]{1,5})?")
_TAG = re.compile(r"[A-Za-z0-9_][A-Za-z0-9._-]{0,127}")
_DIGEST = re.compile(r"sha256:[0-9a-f]{64}")
_CHALLENGE = re.compile(r'(\w+)="([^"]*)"')

_TIMEOUT = 60
_CHUNK = 1024 * 1024
_MAX_DOCUMENT_BYTES = 8 * 1024 * 1024


class RegistryError(Exception):
    """The registry could not be read, or served something unexpected."""


class NotPublished(RegistryError):
    """The registry has no bundle under the tag asked for."""


def tag_for(image_os: str, arch: str, prek_version: str) -> str:
    """Return the tag of the bundle that serves one kind of lint job.

    A bundle fits one runner image, architecture and prek release, so
    all three are in the tag, and a job no bundle fits finds no tag.
    """
    return f"{image_os}-{arch}-prek{prek_version}"


@dataclass(frozen=True)
class Reference:
    """A repository on a registry, and a tag in it."""

    host: str
    name: str
    tag: str

    @staticmethod
    def parse(repository: str, tag: str) -> Reference:
        """Build a reference from 'host/name' and a tag, or raise."""
        host, _, name = repository.partition("/")
        if not (
            _HOST.fullmatch(host) and _NAME.fullmatch(name) and _TAG.fullmatch(tag)
        ):
            raise RegistryError(f"not a registry reference: {repository}:{tag}")
        return Reference(host, name, tag)

    @property
    def local(self) -> bool:
        """Whether the registry is on this machine, as in the tests."""
        return self.host.split(":")[0] in ("localhost", "127.0.0.1")

    @property
    def base(self) -> str:
        """The URL of the repository's API. Plain HTTP is for local use."""
        return f"{'http' if self.local else 'https'}://{self.host}/v2/{self.name}"


def _urlopen(request: urllib.request.Request) -> HTTPResponse:
    """Open an http or https request built from a checked reference."""
    return cast(
        "HTTPResponse",
        urllib.request.urlopen(request, timeout=_TIMEOUT),  # noqa: S310
    )


@final
class Registry:
    """A read-only client for one repository.

    password, when given, is exchanged for a pull token; without it
    the pull is anonymous, which is what a public bundle needs.
    """

    def __init__(self, reference: Reference, password: str = "") -> None:
        """Prepare to read reference; nothing is requested yet."""
        self.reference = reference
        self._password = password
        self._token = ""

    def _open(self, url: str, accept: str) -> HTTPResponse:
        request = urllib.request.Request(url, headers={"Accept": accept})  # noqa: S310
        if self._token:
            # Unredirected: a registry answers a blob request with a
            # redirect to its storage host, which must not see the token.
            request.add_unredirected_header("Authorization", f"Bearer {self._token}")
        return _urlopen(request)

    def _authenticate(self, challenge: str) -> None:
        """Fetch a pull token as a 401's WWW-Authenticate header directs."""
        pairs = cast("list[tuple[str, str]]", _CHALLENGE.findall(challenge))
        fields = dict(pairs)
        realm = fields.get("realm", "")
        schemes = ("https://", "http://") if self.reference.local else ("https://",)
        if not challenge.startswith("Bearer ") or not realm.startswith(schemes):
            raise RegistryError("the registry asks for an unsupported login")
        query = urllib.parse.urlencode(
            {"service": fields.get("service", ""), "scope": fields.get("scope", "")}
        )
        request = urllib.request.Request(f"{realm}?{query}")  # noqa: S310
        if self._password:
            basic = base64.b64encode(f"token:{self._password}".encode()).decode()
            request.add_unredirected_header("Authorization", f"Basic {basic}")
        try:
            with _urlopen(request) as response:
                reply = values.mapping(values.parse(response.read(_MAX_DOCUMENT_BYTES)))
        except (urllib.error.URLError, ValueError) as error:
            raise RegistryError(f"could not get a pull token: {error}") from error
        token = (reply or {}).get("token") or (reply or {}).get("access_token")
        if not isinstance(token, str) or not token:
            raise RegistryError("the registry gave no pull token")
        self._token = token

    def _get(self, path: str, accept: str) -> HTTPResponse:
        """Request a path of the repository, logging in once if asked to."""
        url = f"{self.reference.base}/{path}"
        try:
            try:
                return self._open(url, accept)
            except urllib.error.HTTPError as error:
                error.close()
                if error.code != 401 or self._token:  # noqa: PLR2004
                    raise
                self._authenticate(error.headers.get("WWW-Authenticate", ""))
            return self._open(url, accept)
        except urllib.error.HTTPError as error:
            error.close()
            if error.code == 404:  # noqa: PLR2004
                raise NotPublished(f"{path} is not in the registry") from error
            raise RegistryError(f"{path}: HTTP {error.code}") from error
        except urllib.error.URLError as error:
            raise RegistryError(f"{path}: {error.reason}") from error

    def layers(self) -> dict[str, str]:
        """Return the digest of each layer, keyed by its file name."""
        with self._get(f"manifests/{self.reference.tag}", _MANIFEST_TYPE) as response:
            body = response.read(_MAX_DOCUMENT_BYTES + 1)
        if len(body) > _MAX_DOCUMENT_BYTES:
            raise RegistryError("the manifest is implausibly large")
        try:
            manifest = values.mapping(values.parse(body)) or {}
        except ValueError as error:
            raise RegistryError("the manifest is not JSON") from error
        found: dict[str, str] = {}
        for item in values.sequence(manifest.get("layers")) or []:
            layer = values.mapping(item) or {}
            kind = values.string(layer.get("mediaType"))
            digest = values.string(layer.get("digest")) or ""
            if kind in (INDEX_TYPE, ATTESTATION_TYPE) and _DIGEST.fullmatch(digest):
                found[kind] = digest
        return found

    def download(self, digest: str, target: Path, limit: int) -> None:
        """Save a blob to target, requiring it to hash to its digest.

        Nothing is left at target unless the whole blob arrived, was no
        larger than limit, and matched.
        """
        if not _DIGEST.fullmatch(digest):
            raise RegistryError(f"not a digest: {digest}")
        hasher = hashlib.sha256()
        size = 0
        partial = target.with_name(target.name + ".partial")
        try:
            with (
                self._get(f"blobs/{digest}", "application/octet-stream") as response,
                partial.open("wb") as out,
            ):
                while chunk := response.read(_CHUNK):
                    size += len(chunk)
                    if size > limit:
                        raise RegistryError(f"{digest} is larger than expected")
                    hasher.update(chunk)
                    _ = out.write(chunk)
            if f"sha256:{hasher.hexdigest()}" != digest:
                raise RegistryError(f"{digest}: content does not match")
            _ = partial.replace(target)
        except (OSError, http.client.HTTPException) as error:
            raise RegistryError(f"{digest}: {error}") from error
        finally:
            partial.unlink(missing_ok=True)
