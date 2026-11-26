# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""Pulling a published bundle: what is fetched, and in what order.

The registry here is a small HTTP server speaking the parts of the OCI
distribution API the client uses, and recording what it was asked for,
so the tests can require that an unverified index leads to no further
request at all.
"""

from __future__ import annotations

import hashlib
import json
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import TYPE_CHECKING, final, override

from hookbundles import install, pull, registry, selection
from tests import fixtures
from tests.fixtures import ENV_DIR, REV, RUFF

if TYPE_CHECKING:
    from collections.abc import Callable

_NAME = "example/prek-hook-bundles"
_TAG = "ubuntu24-x86_64-prek0.4.14"
_PUBLISHER = pull.Publisher(
    "example/prek-hook-bundles", "build.yaml", "refs/heads/main"
)
_CONFIG: dict[str, object] = {
    "repos": [{"repo": RUFF, "rev": REV, "hooks": [{"id": "ruff"}]}]
}


def _digest(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


@final
class _Store:
    """What the fake registry serves, and what it has been asked for."""

    def __init__(self) -> None:
        """Start empty, and open to anonymous pulls."""
        self.manifests: dict[str, bytes] = {}
        self.blobs: dict[str, bytes] = {}
        self.login = False
        self.requested: list[str] = []
        self.token_requests: list[str] = []
        self.storage_authorization: list[str | None] = []

    def publish(self, index: dict[str, object], blobs: Path) -> None:
        """Serve a bundle as the publish job pushes it."""
        index_bytes = json.dumps(index).encode()
        attestation = b'{"attestation": true}\n'
        layers = [
            {"mediaType": registry.INDEX_TYPE, "digest": _digest(index_bytes)},
            {"mediaType": registry.ATTESTATION_TYPE, "digest": _digest(attestation)},
        ]
        self.blobs = {
            _digest(index_bytes): index_bytes,
            _digest(attestation): attestation,
        }
        for path in blobs.iterdir():
            data = path.read_bytes()
            self.blobs[_digest(data)] = data
            layers.append({"mediaType": registry.BLOB_TYPE, "digest": _digest(data)})
        self.manifests[_TAG] = json.dumps({"layers": layers}).encode()


def _handler(store: _Store) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        def _send(self, status: int, body: bytes = b"", **headers: str) -> None:
            self.send_response(status)
            for key, value in headers.items():
                self.send_header(key.replace("_", "-"), value)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            _ = self.wfile.write(body)

        def do_GET(self) -> None:  # noqa: N802 - the name http.server calls
            host = f"http://{self.headers.get('Host')}"
            authorization = self.headers.get("Authorization")
            if self.path.startswith("/token"):
                store.token_requests.append(authorization or "")
                self._send(200, b'{"token": "pull-token"}')
            elif self.path.startswith("/storage/"):
                store.storage_authorization.append(authorization)
                blob = store.blobs.get(self.path.removeprefix("/storage/"))
                self._send(200 if blob else 404, blob or b"")
            elif store.login and authorization != "Bearer pull-token":
                challenge = f'Bearer realm="{host}/token",service="fake",scope="pull"'
                self._send(401, WWW_Authenticate=challenge)
            elif self.path.startswith(f"/v2/{_NAME}/manifests/"):
                manifest = store.manifests.get(self.path.rsplit("/", 1)[1])
                self._send(200 if manifest else 404, manifest or b"")
            elif self.path.startswith(f"/v2/{_NAME}/blobs/"):
                digest = self.path.rsplit("/", 1)[1]
                store.requested.append(digest)
                self._send(307, Location=f"{host}/storage/{digest}")
            else:
                self._send(404)

        @override
        def log_message(self, format: str, *args: object) -> None:  # noqa: A002
            """Keep the test output quiet."""

    return Handler


class PullTest(unittest.TestCase):
    """A bundle served by a fake registry, pulled into a temporary store."""

    store: _Store = _Store()
    index: dict[str, object] = {}  # noqa: RUF012 - replaced in setUp
    home: Path = Path()
    work: Path = Path()
    repository: str = ""
    verified: list[tuple[Path, Path, pull.Publisher]] = []  # noqa: RUF012

    @override
    def setUp(self) -> None:
        """Assemble a bundle, and serve it."""
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name).resolve()
        self.index, blobs, self.home = fixtures.bundle(root)
        self.work = root / "pull"
        self.store = _Store()
        self.store.publish(self.index, blobs)
        self.verified = []
        server = ThreadingHTTPServer(("127.0.0.1", 0), _handler(self.store))
        thread = threading.Thread(
            target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True
        )
        thread.start()
        self.addCleanup(thread.join)
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        self.repository = f"127.0.0.1:{server.server_port}/{_NAME}"

    def accept(self, index: Path, attestation: Path, publisher: pull.Publisher) -> None:
        """Stand in for gh: record the call and accept the index."""
        self.verified.append((index, attestation, publisher))

    def pull(
        self,
        verify: Callable[[Path, Path, pull.Publisher], None] | None = None,
        password: str = "",
        changes: dict[str, str] | None = None,
    ) -> dict[str, object]:
        """Pull for a runner the bundle fits, but for the changes given."""
        fit = {"arch": "x86_64", "image_os": "ubuntu24", "prek_version": "0.4.14"}
        fit.update(changes or {})
        request = pull.Request(
            self.repository, _PUBLISHER, _CONFIG, None, self.work, password
        )
        runner = install.Runner(home=self.home, **fit)
        return pull.pull(request, runner, verify or self.accept)

    def environment_blobs(self) -> set[str]:
        """Return the digests of everything but the index and attestation."""
        names = selection.everything(self.index)
        return {"sha256:" + name.split(".", 1)[0] for name in names}

    def assert_nothing_installed(self) -> None:
        """Require the store to hold no file at all."""
        files = [path for path in self.home.rglob("*") if not path.is_dir()]
        self.assertEqual(files, [])

    def test_pull_installs_what_the_hooks_need(self) -> None:
        """A hook's environment arrives, and the seed nobody asked for does not."""
        report = self.pull()
        self.assertTrue((self.home / "hooks" / ENV_DIR / "bin" / "tool").is_file())
        self.assertFalse((self.home / "hooks" / fixtures.SEED_DIR).exists())
        self.assertEqual((report["blobs"], report["environments"]), (3, 1))
        self.assertEqual(report["tag"], _TAG)

    def test_index_is_verified_against_its_attestation(self) -> None:
        """The verifier gets the files as served, and the pinned publisher."""
        _ = self.pull()
        ((index, attestation, publisher),) = self.verified
        self.assertEqual(json.loads(index.read_text()), self.index)
        self.assertEqual(attestation.read_bytes(), b'{"attestation": true}\n')
        self.assertEqual(publisher, _PUBLISHER)

    def test_unverified_index_stops_everything(self) -> None:
        """No blob is fetched, and nothing written, for a rejected index."""

        def reject(*_: object) -> None:
            raise pull.Unverified("not the publisher")

        with self.assertRaises(pull.Unverified):
            _ = self.pull(reject)
        self.assertFalse(self.environment_blobs() & set(self.store.requested))
        self.assertEqual(len(self.store.requested), 2)
        self.assert_nothing_installed()

    def test_no_bundle_for_this_runner(self) -> None:
        """An unpublished tag is not applicable, not an error."""
        for changes in ({"prek_version": "0.4.15"}, {"image_os": "ubuntu22"}):
            with (
                self.subTest(changes=changes),
                self.assertRaises(install.NotApplicable),
            ):
                _ = self.pull(changes=changes)
        self.assertEqual(self.store.requested, [])
        self.assert_nothing_installed()

    def test_index_that_does_not_fit_fetches_no_blob(self) -> None:
        """A tag whose index was built for another store is declined."""
        self.home = self.home.with_name("elsewhere")
        with self.assertRaises(install.NotApplicable):
            _ = self.pull()
        self.assertFalse(self.environment_blobs() & set(self.store.requested))

    def test_altered_blob_is_refused(self) -> None:
        """A blob that does not hash to its digest is never installed."""
        for digest in self.environment_blobs():
            self.store.blobs[digest] += b"\0"
        with self.assertRaisesRegex(registry.RegistryError, "larger|does not match"):
            _ = self.pull()
        self.assert_nothing_installed()

    def test_bundle_without_an_attestation_is_refused(self) -> None:
        """A manifest lacking either document is not a bundle."""
        layer = {"mediaType": registry.INDEX_TYPE, "digest": "sha256:" + "0" * 64}
        self.store.manifests[_TAG] = json.dumps({"layers": [layer]}).encode()
        with self.assertRaisesRegex(registry.RegistryError, "attestation"):
            _ = self.pull()
        self.assertEqual(self.store.requested, [])

    def test_token_is_fetched_when_asked_and_kept_from_storage(self) -> None:
        """A login challenge is answered, and storage never sees the token."""
        self.store.login = True
        _ = self.pull()
        self.assertEqual(self.store.token_requests, [""])
        self.assertTrue(self.store.storage_authorization)
        self.assertEqual(set(self.store.storage_authorization), {None})

    def test_password_goes_to_the_token_endpoint_alone(self) -> None:
        """A registry password is exchanged for a token, as Basic auth."""
        self.store.login = True
        _ = self.pull(password="secret")  # noqa: S106 - a test value
        (authorization,) = self.store.token_requests
        self.assertTrue(authorization.startswith("Basic "))
        self.assertEqual(set(self.store.storage_authorization), {None})


class ReferenceTest(unittest.TestCase):
    """Names, tags and identities."""

    def test_tag_names_image_architecture_and_prek(self) -> None:
        """A job's three properties make the tag it looks for."""
        self.assertEqual(registry.tag_for("ubuntu24", "x86_64", "0.4.14"), _TAG)

    def test_malformed_references_are_refused(self) -> None:
        """Nothing but a host, a lower-case path and a plain tag passes."""
        for repository, tag in (
            ("ghcr.io", _TAG),
            ("ghcr.io/Owner/name", _TAG),
            ("ghcr.io/owner/name", "bad tag"),
            ("ghcr.io/owner/../name", _TAG),
            ("https://ghcr.io/owner/name", _TAG),
            ("ghcr.io/owner/name?x=1", _TAG),
        ):
            with (
                self.subTest(repository=repository),
                self.assertRaises(registry.RegistryError),
            ):
                _ = registry.Reference.parse(repository, tag)

    def test_remote_registries_use_https(self) -> None:
        """Plain HTTP is for a registry on this machine and no other."""
        remote = registry.Reference.parse("ghcr.io/owner/name", _TAG)
        local = registry.Reference.parse("localhost:5000/owner/name", _TAG)
        self.assertTrue(remote.base.startswith("https://ghcr.io/v2/"))
        self.assertTrue(local.base.startswith("http://localhost:5000/v2/"))

    def test_publisher_identity(self) -> None:
        """The identity is the workflow's URL at the ref it ran on."""
        publisher = pull.Publisher(
            "lfreleng-actions/prek-hook-bundles",
            ".github/workflows/build.yaml",
            "refs/heads/main",
        )
        self.assertEqual(
            publisher.identity,
            "https://github.com/lfreleng-actions/prek-hook-bundles"
            + "/.github/workflows/build.yaml@refs/heads/main",
        )


if __name__ == "__main__":
    _ = unittest.main()
