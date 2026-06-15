from __future__ import annotations

import base64
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import patch

from apt_index import cli
from apt_index.config import ConfigError, load_configuration


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


def package_entry() -> dict[str, object]:
    return {
        "homepage": "https://example.test/pkg",
        "architectures": {
            "amd64": {
                "update_policy": "track",
                "source": {
                    "type": "github",
                    "repo": "example/pkg",
                    "asset_pattern": "pkg_*_amd64.deb",
                },
            }
        },
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


def write_repository_config(root: Path) -> None:
    root.joinpath("apt-index.toml").write_text(
        """
suite = "stable"
component = "main"

[repository]
origin = "Apt Index"
label = "test index"
description = "Test index"
base_url = "https://example.test"

[signing]
key_name = "Apt Index <apt-index@example.test>"
""".strip()
        + "\n",
        encoding="utf-8",
    )


class ConfigLoadingTests(unittest.TestCase):
    def test_shorthand_entry_expands_to_per_architecture_runtime_config(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_repository_config(root)
            packages = root / "packages"
            packages.mkdir()
            packages.joinpath("bat.toml").write_text(
                """
homepage = "https://github.com/sharkdp/bat"
architectures = ["amd64", "arm64"]
source = "github"
update_policy = "track"

[sources.github]
repo = "sharkdp/bat"

[sources.github.asset_patterns]
amd64 = "bat_*_amd64.deb"
arm64 = "bat_*_arm64.deb"
""".strip()
                + "\n",
                encoding="utf-8",
            )

            config = load_configuration(root).to_runtime_dict()

        self.assertEqual(config["packages"]["bat"]["architectures"].keys(), {"amd64", "arm64"})
        amd64 = config["packages"]["bat"]["architectures"]["amd64"]
        self.assertEqual(amd64["update_policy"], "track")
        self.assertEqual(amd64["source"], {"type": "github", "repo": "sharkdp/bat", "asset_pattern": "bat_*_amd64.deb"})

    def test_explicit_mixed_source_entry_normalizes_selected_sources_only(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_repository_config(root)
            package_dir = root / "packages" / "mixed"
            package_dir.mkdir(parents=True)
            package_dir.joinpath("index.toml").write_text(
                """
homepage = "https://example.test/pkg"

[architectures]
amd64 = { source = "aur", update_policy = "track" }
arm64 = { source = "github", update_policy = "fixed" }

[sources.aur]
package = "example-bin"

[sources.aur.asset_patterns]
amd64 = "example_*_amd64.deb"

[sources.github]
repo = "example/pkg"

[sources.github.asset_patterns]
arm64 = "pkg_*_arm64.deb"

[sources.github.release_tags]
arm64 = "v1.2.3"
""".strip()
                + "\n",
                encoding="utf-8",
            )

            config = load_configuration(root).to_runtime_dict()

        entry = config["packages"]["mixed"]
        self.assertEqual(entry["architectures"]["amd64"]["source"], {"type": "aur", "package": "example-bin", "asset_pattern": "example_*_amd64.deb"})
        self.assertEqual(
            entry["architectures"]["arm64"]["source"],
            {"type": "github", "repo": "example/pkg", "asset_pattern": "pkg_*_arm64.deb", "release_tag": "v1.2.3"},
        )

    def test_unselected_source_option_is_accepted_and_dropped(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_repository_config(root)
            packages = root / "packages"
            packages.mkdir()
            packages.joinpath("pkg.toml").write_text(
                """
homepage = "https://example.test/pkg"
architectures = ["amd64"]
source = "github"
update_policy = "track"

[sources.github]
repo = "example/pkg"

[sources.github.asset_patterns]
amd64 = "pkg_*_amd64.deb"

[sources.aur]
package = "unused-bin"

[sources.aur.asset_patterns]
amd64 = "unused_*_amd64.deb"
""".strip()
                + "\n",
                encoding="utf-8",
            )

            config = load_configuration(root).to_runtime_dict()

        self.assertEqual(config["packages"]["pkg"]["architectures"]["amd64"]["source"]["type"], "github")
        self.assertNotIn("sources", config["packages"]["pkg"])

    def test_flat_old_resolver_fields_fail_validation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_repository_config(root)
            packages = root / "packages"
            packages.mkdir()
            packages.joinpath("pkg.toml").write_text(
                """
homepage = "https://example.test/pkg"
architectures = ["amd64"]
source = "github"
update_policy = "track"
repo = "example/pkg"
""".strip()
                + "\n",
                encoding="utf-8",
            )

            with self.assertRaisesRegex(ConfigError, "Extra inputs are not permitted"):
                load_configuration(root)

    def test_release_tag_and_release_tags_cannot_be_combined(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_repository_config(root)
            packages = root / "packages"
            packages.mkdir()
            packages.joinpath("pkg.toml").write_text(
                """
homepage = "https://example.test/pkg"
architectures = ["amd64"]
source = "github"
update_policy = "fixed"

[sources.github]
repo = "example/pkg"
release_tag = "v1"

[sources.github.asset_patterns]
amd64 = "pkg_*_amd64.deb"

[sources.github.release_tags]
amd64 = "v1"
""".strip()
                + "\n",
                encoding="utf-8",
            )

            with self.assertRaisesRegex(ConfigError, "use release_tag or release_tags"):
                load_configuration(root)

    def test_old_packages_toml_entry_point_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_repository_config(root)
            root.joinpath("packages.toml").write_text("suite = \"stable\"\n", encoding="utf-8")

            with self.assertRaisesRegex(ConfigError, "packages.toml is not a valid configuration entry point"):
                load_configuration(root)

    def test_duplicate_entry_layouts_fail(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_repository_config(root)
            packages = root / "packages"
            packages.joinpath("pkg").mkdir(parents=True)
            packages.joinpath("pkg.toml").write_text("homepage = \"https://example.test\"\n", encoding="utf-8")
            packages.joinpath("pkg", "index.toml").write_text("homepage = \"https://example.test\"\n", encoding="utf-8")

            with self.assertRaisesRegex(ConfigError, "duplicate software entry"):
                load_configuration(root)

    def test_nested_entry_paths_fail(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_repository_config(root)
            nested = root / "packages" / "vendor" / "pkg"
            nested.mkdir(parents=True)
            nested.joinpath("index.toml").write_text("homepage = \"https://example.test\"\n", encoding="utf-8")

            with self.assertRaisesRegex(ConfigError, "nested entry path is not allowed"):
                load_configuration(root)


class ResolveEntryTests(unittest.TestCase):
    def test_reuses_locked_artifact_when_candidate_is_unchanged(self) -> None:
        previous_entry = {
            "homepage": "https://example.test/pkg",
            "architectures": {
                "amd64": {
                    "source": "github",
                    "update_policy": "track",
                    "resolved_at": "previous",
                    "artifact": locked_artifact(),
                }
            },
        }
        candidate = cli.ArtifactCandidate(
            "https://example.test/pkg.deb",
            "1.0.0",
            "pkg_1.0.0_amd64.deb",
        )

        with patch.object(cli, "resolve_candidate", return_value=candidate), patch.object(cli, "download") as download:
            resolved = cli.resolve_entry("pkg", package_entry(), previous_entry)

        download.assert_not_called()
        self.assertEqual(resolved.entry, previous_entry)
        self.assertEqual(resolved.full_checked_arches, set())

    def test_downloads_and_updates_artifact_when_candidate_changes(self) -> None:
        previous_entry = {
            "homepage": "https://example.test/pkg",
            "architectures": {
                "amd64": {
                    "source": "github",
                    "update_policy": "track",
                    "resolved_at": "previous",
                    "artifact": locked_artifact(),
                }
            },
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
            resolved = cli.resolve_entry("pkg", package_entry(), previous_entry)

        download.assert_called_once_with("https://example.test/pkg-2.deb", None, "sha256")
        artifact = resolved.entry["architectures"]["amd64"]["artifact"]
        self.assertEqual(artifact["url"], "https://example.test/pkg-2.deb")
        self.assertEqual(artifact["sha256"], "new-sha256")
        self.assertEqual(resolved.full_checked_arches, {"amd64"})

    def test_keeps_previous_architecture_when_refresh_fails(self) -> None:
        previous_entry = {
            "homepage": "https://example.test/pkg",
            "architectures": {
                "amd64": {
                    "source": "github",
                    "update_policy": "track",
                    "resolved_at": "previous",
                    "artifact": locked_artifact(),
                }
            },
        }

        with patch.object(cli, "resolve_candidate", side_effect=RuntimeError("no asset")):
            resolved = cli.resolve_entry("pkg", package_entry(), previous_entry)

        self.assertEqual(resolved.entry["architectures"]["amd64"], previous_entry["architectures"]["amd64"])
        self.assertEqual(resolved.architecture_health["amd64"]["status"], "kept_previous")
        self.assertEqual(resolved.architecture_health["amd64"]["error"], "no asset")

    def test_one_architecture_can_update_while_another_fails(self) -> None:
        entry = package_entry()
        entry["architectures"]["arm64"] = {
            "update_policy": "track",
            "source": {
                "type": "github",
                "repo": "example/pkg",
                "asset_pattern": "pkg_*_arm64.deb",
            },
        }
        candidates = {
            "amd64": cli.ArtifactCandidate("https://example.test/pkg-amd64.deb", "1.0.0", "pkg_1.0.0_amd64.deb"),
        }

        def fake_resolve_candidate(architecture: dict[str, object]) -> cli.ArtifactCandidate:
            source = architecture["source"]
            pattern = source["asset_pattern"]
            if "arm64" in pattern:
                raise RuntimeError("no arm64 asset")
            return candidates["amd64"]

        def fake_inspect_deb(path: Path) -> dict[str, object]:
            return {
                "control": {"Package": "pkg", "Version": "1.0.0", "Architecture": "amd64"},
                "size": 123,
                "md5": "md5",
                "sha1": "sha1",
                "sha256": "sha256-amd64",
            }

        with (
            patch.object(cli, "resolve_candidate", side_effect=fake_resolve_candidate),
            patch.object(cli, "download", side_effect=lambda url, expected_hash, hash_algorithm: Path("/tmp") / Path(url).name),
            patch.object(cli, "inspect_deb", side_effect=fake_inspect_deb),
        ):
            resolved = cli.resolve_entry("pkg", entry)

        self.assertEqual(resolved.entry["architectures"].keys(), {"amd64"})
        self.assertEqual(resolved.entry["architectures"]["amd64"]["artifact"]["sha256"], "sha256-amd64")
        self.assertEqual(resolved.architecture_health["arm64"]["status"], "failed")
        self.assertEqual(resolved.full_checked_arches, {"amd64"})


class ResolveCandidateTests(unittest.TestCase):
    def test_aur_generic_source_selects_deb_and_matching_sha512(self) -> None:
        srcinfo = """
pkgbase = google-chrome
    pkgver = 149.0.7827.114
    arch = x86_64
    source = https://dl.google.com/linux/chrome/deb/pool/main/g/google-chrome-stable/google-chrome-stable_149.0.7827.114-1_amd64.deb
    source = eula_text.html
    source = google-chrome-stable.sh
    sha512sums = deb-sha512
    sha512sums = eula-sha512
    sha512sums = script-sha512
""".strip()

        architecture = {
            "update_policy": "track",
            "source": {
                "type": "aur",
                "package": "google-chrome",
                "asset_pattern": "google-chrome-stable_*_amd64.deb",
            },
        }

        with patch.object(cli, "fetch_text", return_value=srcinfo):
            candidate = cli.resolve_candidate(architecture)

        self.assertEqual(candidate.url, "https://dl.google.com/linux/chrome/deb/pool/main/g/google-chrome-stable/google-chrome-stable_149.0.7827.114-1_amd64.deb")
        self.assertEqual(candidate.asset_name, "google-chrome-stable_149.0.7827.114-1_amd64.deb")
        self.assertEqual(candidate.upstream_version, "149.0.7827.114")
        self.assertEqual(candidate.expected_hash, "deb-sha512")
        self.assertEqual(candidate.hash_algorithm, "sha512")

    def test_aur_asset_pattern_matches_arch_specific_source_and_sha256(self) -> None:
        srcinfo = """
pkgbase = example-bin
    pkgver = 1.2.3
    source = helper.txt
    sha256sums = helper-sha256
    source_aarch64 = example.deb::https://example.test/example-arm64.deb
    sha256sums_aarch64 = deb-sha256
""".strip()

        architecture = {
            "update_policy": "track",
            "source": {
                "type": "aur",
                "package": "example-bin",
                "asset_pattern": "example.deb",
            },
        }

        with patch.object(cli, "fetch_text", return_value=srcinfo):
            candidate = cli.resolve_candidate(architecture)

        self.assertEqual(candidate.url, "https://example.test/example-arm64.deb")
        self.assertEqual(candidate.asset_name, "example.deb")
        self.assertEqual(candidate.expected_hash, "deb-sha256")
        self.assertEqual(candidate.hash_algorithm, "sha256")


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
        lock = {
            "packages": {
                "pkg": {
                    "architectures": {
                        "amd64": {
                            "source": "github",
                            "update_policy": "track",
                            "artifact": locked_artifact(),
                        }
                    }
                }
            }
        }

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
