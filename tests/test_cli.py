from __future__ import annotations

import base64
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

    def test_skips_optional_architecture_without_selector(self) -> None:
        config = {"required_architectures": ["amd64"], "optional_architectures": ["arm64"]}
        entry = package_entry() | {"asset_patterns": {"amd64": "pkg_*_amd64.deb"}}
        candidate = cli.ArtifactCandidate(
            "https://example.test/pkg.deb",
            "1.0.0",
            "pkg_1.0.0_amd64.deb",
        )

        with patch.object(cli, "resolve_candidate", return_value=candidate) as resolve_candidate:
            resolved = cli.resolve_entry(config, "pkg", entry, {"artifacts": {"amd64": locked_artifact()}})

        resolve_candidate.assert_called_once_with(entry, "amd64")
        self.assertEqual(resolved.entry["artifacts"].keys(), {"amd64"})

    def test_resolves_optional_architecture_with_selector(self) -> None:
        config = {"required_architectures": ["amd64"], "optional_architectures": ["arm64"]}
        entry = package_entry() | {"asset_patterns": {"amd64": "pkg_*_amd64.deb", "arm64": "pkg_*_arm64.deb"}}
        candidates = {
            "amd64": cli.ArtifactCandidate("https://example.test/pkg-amd64.deb", "1.0.0", "pkg_1.0.0_amd64.deb"),
            "arm64": cli.ArtifactCandidate("https://example.test/pkg-arm64.deb", "1.0.0", "pkg_1.0.0_arm64.deb"),
        }

        def fake_resolve_candidate(entry: dict[str, object], arch: str) -> cli.ArtifactCandidate:
            return candidates[arch]

        def fake_inspect_deb(path: Path) -> dict[str, object]:
            arch = "arm64" if path.name == "pkg-arm64.deb" else "amd64"
            return {
                "control": {"Package": "pkg", "Version": "1.0.0", "Architecture": arch},
                "size": 123,
                "md5": "md5",
                "sha1": "sha1",
                "sha256": f"sha256-{arch}",
            }

        with (
            patch.object(cli, "resolve_candidate", side_effect=fake_resolve_candidate),
            patch.object(cli, "download", side_effect=lambda url, _: Path("/tmp") / Path(url).name),
            patch.object(cli, "inspect_deb", side_effect=fake_inspect_deb),
        ):
            resolved = cli.resolve_entry(config, "pkg", entry)

        self.assertEqual(resolved.entry["artifacts"].keys(), {"amd64", "arm64"})
        self.assertEqual(resolved.entry["artifacts"]["arm64"]["sha256"], "sha256-arm64")
        self.assertEqual(resolved.full_checked_arches, {"amd64", "arm64"})


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


class DownloadStatsTests(unittest.TestCase):
    def test_formats_download_stats_for_public_json(self) -> None:
        stats = cli.format_download_stats(
            [{"entry_name": "bat", "arch": "amd64", "downloads": 12}],
            [{"entry_name": "bat", "arch": "amd64", "downloads": 3}],
            [{"day": "2026-06-14T00:00:00Z", "downloads": 5}],
            [{"downloads": 20}],
            "apt_index_downloads",
            30,
        )

        self.assertEqual(stats["source"], "workers_analytics_engine")
        self.assertEqual(stats["dataset"], "apt_index_downloads")
        self.assertEqual(stats["totals"], {"downloads": 20, "last_days": 12, "last_7_days": 3})
        self.assertEqual(stats["packages"], [{"entry_name": "bat", "arch": "amd64", "downloads": 12, "last_7_days": 3}])
        self.assertEqual(stats["daily"], [{"date": "2026-06-14", "downloads": 5}])

    def test_write_download_stats_writes_empty_file_without_cloudflare_credentials(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, patch.dict(cli.os.environ, {}, clear=True):
            output = Path(tmp) / "nested" / "download_stats.json"
            cli.write_download_stats(output, days=14)

            stats = cli.load_json(output, None)

        self.assertEqual(stats["source"], "none")
        self.assertEqual(stats["reason"], "missing_cloudflare_credentials")
        self.assertEqual(stats["window_days"], 14)

    def test_write_download_stats_writes_empty_file_when_query_fails(self) -> None:
        env = {"CLOUDFLARE_ACCOUNT_ID": "account", "CLOUDFLARE_API_TOKEN": "token"}
        with (
            tempfile.TemporaryDirectory() as tmp,
            patch.dict(cli.os.environ, env, clear=True),
            patch.object(cli, "fetch_download_stats", side_effect=RuntimeError("dataset not found")),
        ):
            output = Path(tmp) / "download_stats.json"
            cli.write_download_stats(output)

            stats = cli.load_json(output, None)

        self.assertEqual(stats["source"], "none")
        self.assertEqual(stats["reason"], "analytics_query_failed")


class WorkerGenerationTests(unittest.TestCase):
    def test_worker_records_optional_download_analytics_before_redirect(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "_worker.js"
            cli.write_worker(path)

            worker = path.read_text(encoding="utf-8")

        self.assertIn("env.DOWNLOADS.writeDataPoint", worker)
        self.assertIn("request.method", worker)
        self.assertIn("Response.redirect(target, 302)", worker)


class SigningKeyTests(unittest.TestCase):
    def test_loads_signing_environment_from_dotenv(self) -> None:
        with (
            tempfile.TemporaryDirectory() as tmp,
            patch.object(cli, "ENV_PATH", Path(tmp) / ".env"),
            patch.object(cli, "DOTENV_LOADED", False),
            patch.dict(cli.os.environ, {}, clear=True),
        ):
            cli.ENV_PATH.write_text(
                f'export {cli.SIGNING_PRIVATE_KEY_B64_ENV}="encoded"\n{cli.SIGNING_PASSPHRASE_ENV}=secret\n',
                encoding="utf-8",
            )

            cli.load_dotenv()

            self.assertEqual(cli.os.environ[cli.SIGNING_PRIVATE_KEY_B64_ENV], "encoded")
            self.assertEqual(cli.os.environ[cli.SIGNING_PASSPHRASE_ENV], "secret")

    def test_imports_private_key_from_environment_when_secret_key_is_missing(self) -> None:
        class Result:
            def __init__(self, returncode: int = 0, stdout: str = "") -> None:
                self.returncode = returncode
                self.stdout = stdout

        key_material = "-----BEGIN PGP PRIVATE KEY BLOCK-----\nkey\n-----END PGP PRIVATE KEY BLOCK-----\n"
        env = {
            cli.SIGNING_PRIVATE_KEY_B64_ENV: base64.b64encode(key_material.encode("utf-8")).decode("ascii"),
            cli.SIGNING_PASSPHRASE_ENV: "secret",
        }
        calls: list[list[str]] = []
        inputs: list[str | None] = []

        def fake_run(args: list[str], **kwargs: object) -> Result:
            calls.append(args)
            inputs.append(kwargs.get("input"))
            if args[:2] == ["gpg", "--list-secret-keys"]:
                return Result(2 if len([call for call in calls if call[:2] == ["gpg", "--list-secret-keys"]]) == 1 else 0)
            if args[:2] == ["gpg", "--armor"]:
                return Result(stdout="public-key")
            return Result()

        with (
            tempfile.TemporaryDirectory() as tmp,
            patch.object(cli, "GNUPG_DIR", Path(tmp) / "gnupg"),
            patch.object(cli, "ENV_PATH", Path(tmp) / ".env"),
            patch.object(cli, "DOTENV_LOADED", False),
            patch.dict(cli.os.environ, env, clear=True),
            patch.object(cli.subprocess, "run", side_effect=fake_run),
        ):
            public_key = cli.ensure_signing_key({"signing": {"key_name": "Apt Index <apt-index@lyk-ai.com>"}})

        self.assertEqual(public_key, "public-key")
        self.assertIn(
            ["gpg", "--batch", "--yes", "--pinentry-mode", "loopback", "--passphrase", "secret", "--import"],
            calls,
        )
        self.assertIn(key_material, inputs)
        self.assertNotIn("--quick-generate-key", [arg for call in calls for arg in call])

    def test_fails_without_existing_or_configured_private_key(self) -> None:
        class Result:
            returncode = 2
            stdout = ""

        with (
            tempfile.TemporaryDirectory() as tmp,
            patch.object(cli, "GNUPG_DIR", Path(tmp) / "gnupg"),
            patch.object(cli, "ENV_PATH", Path(tmp) / ".env"),
            patch.object(cli, "DOTENV_LOADED", False),
            patch.dict(cli.os.environ, {}, clear=True),
            patch.object(cli.subprocess, "run", return_value=Result()),
        ):
            with self.assertRaisesRegex(RuntimeError, "missing signing private key"):
                cli.ensure_signing_key({"signing": {"key_name": "Apt Index <apt-index@lyk-ai.com>"}})


if __name__ == "__main__":
    unittest.main()
