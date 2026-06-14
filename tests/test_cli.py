from __future__ import annotations

import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import patch

from apt_index import cli


class FakeResponse:
    def __init__(self, headers: dict[str, str], status: int = 200) -> None:
        self.headers = headers
        self.status = status

    def __enter__(self) -> FakeResponse:
        return self

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        return None

    def getheader(self, name: str) -> str | None:
        return self.headers.get(name)


def package_config() -> dict[str, object]:
    return {
        "required_architectures": ["amd64"],
        "optional_architectures": [],
    }


def package_entry() -> dict[str, object]:
    return {
        "update_policy": "track",
        "source": "github",
        "homepage": "https://example.test/pkg",
    }


def locked_artifact() -> dict[str, object]:
    return {
        "url": "https://example.test/pkg.deb",
        "upstream_version": "1.0.0",
        "asset_name": "pkg_1.0.0_amd64.deb",
        "filename": "pkg_1.0.0_amd64.deb",
        "control": {"Package": "pkg", "Version": "1.0.0", "Architecture": "amd64"},
        "size": 123,
        "md5": "md5",
        "sha1": "sha1",
        "sha256": "sha256",
    }


class ResolveEntryTests(unittest.TestCase):
    def test_reuses_locked_artifact_when_candidate_is_unchanged(self) -> None:
        previous_entry = {
            "update_policy": "track",
            "source": "github",
            "homepage": "https://example.test/pkg",
            "resolved_at": "previous",
            "artifacts": {"amd64": locked_artifact()},
        }
        candidate = cli.ArtifactCandidate(
            "https://example.test/pkg.deb",
            "1.0.0",
            "pkg_1.0.0_amd64.deb",
        )

        with patch.object(cli, "resolve_candidate", return_value=candidate), patch.object(cli, "download") as download:
            resolved = cli.resolve_entry(package_config(), "pkg", package_entry(), previous_entry)

        download.assert_not_called()
        self.assertEqual(resolved.entry, previous_entry)
        self.assertEqual(resolved.full_checked_arches, set())

    def test_downloads_and_updates_artifact_when_candidate_changes(self) -> None:
        previous_entry = {
            "update_policy": "track",
            "source": "github",
            "homepage": "https://example.test/pkg",
            "resolved_at": "previous",
            "artifacts": {"amd64": locked_artifact()},
        }
        candidate = cli.ArtifactCandidate(
            "https://example.test/pkg-2.deb",
            "2.0.0",
            "pkg_2.0.0_amd64.deb",
        )
        metadata = {
            "control": {"Package": "pkg", "Version": "2.0.0", "Architecture": "amd64"},
            "size": 456,
            "md5": "new-md5",
            "sha1": "new-sha1",
            "sha256": "new-sha256",
        }

        with (
            patch.object(cli, "resolve_candidate", return_value=candidate),
            patch.object(cli, "download", return_value=Path("/tmp/pkg.deb")) as download,
            patch.object(cli, "inspect_deb", return_value=metadata),
        ):
            resolved = cli.resolve_entry(package_config(), "pkg", package_entry(), previous_entry)

        download.assert_called_once_with("https://example.test/pkg-2.deb", None)
        self.assertEqual(resolved.entry["artifacts"]["amd64"]["url"], "https://example.test/pkg-2.deb")
        self.assertEqual(resolved.entry["artifacts"]["amd64"]["sha256"], "new-sha256")
        self.assertEqual(resolved.full_checked_arches, {"amd64"})


class ArtifactHealthTests(unittest.TestCase):
    def test_light_health_uses_head_and_compares_content_length(self) -> None:
        artifact = locked_artifact()

        def fake_urlopen(request: object, timeout: int = 0) -> FakeResponse:
            self.assertEqual(request.get_method(), "HEAD")
            self.assertEqual(timeout, 60)
            return FakeResponse({"Content-Length": "123"})

        with patch.object(cli.urllib.request, "urlopen", side_effect=fake_urlopen):
            result = cli.check_artifact_light(artifact)

        self.assertEqual(result, {"status": "ok", "check": "head", "size": 123})

    def test_light_health_falls_back_to_range_when_head_fails(self) -> None:
        artifact = locked_artifact()
        calls: list[str] = []

        def fake_urlopen(request: object, timeout: int = 0) -> FakeResponse:
            calls.append(request.get_method())
            if request.get_method() == "HEAD":
                raise urllib.error.HTTPError(artifact["url"], 405, "Method Not Allowed", {}, None)
            self.assertEqual(request.headers["Range"], "bytes=0-0")
            return FakeResponse({"Content-Range": "bytes 0-0/123"})

        with patch.object(cli.urllib.request, "urlopen", side_effect=fake_urlopen):
            result = cli.check_artifact_light(artifact)

        self.assertEqual(calls, ["HEAD", "GET"])
        self.assertEqual(result, {"status": "ok", "check": "range", "size": 123})

    def test_range_health_ignores_partial_content_length_without_total_size(self) -> None:
        artifact = locked_artifact()

        def fake_urlopen(request: object, timeout: int = 0) -> FakeResponse:
            if request.get_method() == "HEAD":
                raise urllib.error.HTTPError(artifact["url"], 405, "Method Not Allowed", {}, None)
            return FakeResponse({"Content-Length": "1"}, status=206)

        with patch.object(cli.urllib.request, "urlopen", side_effect=fake_urlopen):
            result = cli.check_artifact_light(artifact)

        self.assertEqual(result, {"status": "ok", "check": "range"})

    def test_full_health_downloads_and_hashes_artifact(self) -> None:
        artifact = locked_artifact()
        with tempfile.NamedTemporaryFile() as tmp:
            path = Path(tmp.name)
            path.write_bytes(b"abc")
            artifact["size"] = 3
            artifact["sha256"] = "actual-sha256"
            with (
                patch.object(cli, "download", return_value=path) as download,
                patch.object(cli, "file_hash", return_value="actual-sha256") as file_hash,
            ):
                result = cli.check_artifact(artifact)

        download.assert_called_once_with("https://example.test/pkg.deb", "actual-sha256")
        file_hash.assert_called_once_with(path, "sha256")
        self.assertEqual(result, {"status": "ok", "check": "full", "size": 3, "sha256": "actual-sha256"})

    def test_check_artifacts_uses_full_check_when_requested(self) -> None:
        lock = {"packages": {"pkg": {"artifacts": {"amd64": locked_artifact()}}}}

        with patch.object(cli, "check_artifact", return_value={"status": "ok", "check": "full"}) as check_artifact:
            health = cli.check_artifacts(lock, jobs=1, full_artifact_check=True)

        check_artifact.assert_called_once()
        self.assertEqual(health["packages"]["pkg"]["artifacts"]["amd64"], {"status": "ok", "check": "full"})


if __name__ == "__main__":
    unittest.main()
