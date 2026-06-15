from __future__ import annotations

import base64
import json
import tempfile
import unittest
import urllib.error
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import Mock, call, patch

from apt_index import cli, deb, download_stats, health as health_module, paths, publish, redirect, site_data as site_data_module, sources
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
        "sha512": "sha512",
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

    def test_sourceforge_entry_normalizes_regex_source_per_architecture(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_repository_config(root)
            packages = root / "packages"
            packages.mkdir()
            packages.joinpath("deadbeef.toml").write_text(
                """
homepage = "https://deadbeef.sourceforge.io/"
architectures = ["amd64", "arm64"]
source = "sourceforge"
update_policy = "track"

[sources.sourceforge]
project = "deadbeef"
path = "Builds/master/linux"

[sources.sourceforge.asset_regexes]
amd64 = "deadbeef-static_.+_amd64\\\\.deb"
arm64 = "deadbeef-static_.+_arm64\\\\.deb"
""".strip()
                + "\n",
                encoding="utf-8",
            )

            config = load_configuration(root).to_runtime_dict()

        self.assertEqual(
            config["packages"]["deadbeef"]["architectures"]["amd64"]["source"],
            {
                "type": "sourceforge",
                "project": "deadbeef",
                "path": "Builds/master/linux",
                "asset_regex": "deadbeef-static_.+_amd64\\.deb",
            },
        )

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


class PathResolutionTests(unittest.TestCase):
    def test_current_project_directory_takes_priority_over_package_location(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            root.joinpath("apt-index.toml").write_text("", encoding="utf-8")
            with patch.object(paths.Path, "cwd", return_value=root):
                self.assertEqual(paths.resolve_root(), root)


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
        candidate = sources.ArtifactCandidate(
            "https://example.test/pkg.deb",
            "1.0.0",
            "pkg_1.0.0_amd64.deb",
        )

        with patch.object(sources, "resolve_candidate", return_value=candidate), patch.object(deb, "download") as download:
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
        candidate = sources.ArtifactCandidate(
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
            "sha512": "new-sha512",
        }

        with (
            patch.object(sources, "resolve_candidate", return_value=candidate),
            patch.object(deb, "download", return_value=Path("/tmp/pkg.deb")) as download,
            patch.object(deb, "inspect_deb", return_value=metadata),
        ):
            resolved = cli.resolve_entry("pkg", package_entry(), previous_entry)

        download.assert_called_once_with(
            "https://example.test/pkg-2.deb",
            cache_dir=cli.CACHE_DIR,
            user_agent=cli.USER_AGENT,
            expected_hash=None,
            hash_algorithm="sha256",
        )
        artifact = resolved.entry["architectures"]["amd64"]["artifact"]
        self.assertEqual(artifact["url"], "https://example.test/pkg-2.deb")
        self.assertEqual(artifact["sha256"], "new-sha256")
        self.assertEqual(artifact["sha512"], "new-sha512")
        self.assertEqual(resolved.full_checked_arches, {"amd64"})

    def test_redownloads_when_candidate_checksum_changes(self) -> None:
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
        candidate = sources.ArtifactCandidate(
            "https://example.test/pkg.deb",
            "1.0.0",
            "pkg_1.0.0_amd64.deb",
            "different-sha1",
            "sha1",
        )
        metadata = {
            "control": {"Package": "pkg", "Version": "1.0.0", "Architecture": "amd64"},
            "size": 456,
            "md5": "new-md5",
            "sha1": "different-sha1",
            "sha256": "new-sha256",
            "sha512": "new-sha512",
        }

        with (
            patch.object(sources, "resolve_candidate", return_value=candidate),
            patch.object(deb, "download", return_value=Path("/tmp/pkg.deb")) as download,
            patch.object(deb, "inspect_deb", return_value=metadata),
        ):
            resolved = cli.resolve_entry("pkg", package_entry(), previous_entry)

        download.assert_called_once_with(
            "https://example.test/pkg.deb",
            cache_dir=cli.CACHE_DIR,
            user_agent=cli.USER_AGENT,
            expected_hash="different-sha1",
            hash_algorithm="sha1",
        )
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

        with patch.object(sources, "resolve_candidate", side_effect=RuntimeError("no asset")):
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
            "amd64": sources.ArtifactCandidate("https://example.test/pkg-amd64.deb", "1.0.0", "pkg_1.0.0_amd64.deb"),
        }

        def fake_resolve_candidate(architecture: dict[str, object], **kwargs: object) -> sources.ArtifactCandidate:
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
                "sha512": "sha512-amd64",
            }

        with (
            patch.object(sources, "resolve_candidate", side_effect=fake_resolve_candidate),
            patch.object(deb, "download", side_effect=lambda url, **kwargs: Path("/tmp") / Path(url).name),
            patch.object(deb, "inspect_deb", side_effect=fake_inspect_deb),
        ):
            resolved = cli.resolve_entry("pkg", entry)

        self.assertEqual(resolved.entry["architectures"].keys(), {"amd64"})
        self.assertEqual(resolved.entry["architectures"]["amd64"]["artifact"]["sha256"], "sha256-amd64")
        self.assertEqual(resolved.architecture_health["arm64"]["status"], "failed")
        self.assertEqual(resolved.full_checked_arches, {"amd64"})

    def test_reuses_locked_artifact_when_aur_sha512_candidate_is_unchanged(self) -> None:
        previous_entry = {
            "homepage": "https://example.test/pkg",
            "architectures": {
                "amd64": {
                    "source": "aur",
                    "update_policy": "track",
                    "resolved_at": "previous",
                    "artifact": {
                        **locked_artifact(),
                        "sha512": "aur-sha512",
                    },
                }
            },
        }
        entry = {
            "homepage": "https://example.test/pkg",
            "architectures": {
                "amd64": {
                    "update_policy": "track",
                    "source": {
                        "type": "aur",
                        "package": "example-bin",
                        "asset_pattern": "pkg_*_amd64.deb",
                    },
                }
            },
        }
        candidate = sources.ArtifactCandidate(
            "https://example.test/pkg.deb",
            "1.0.0",
            "pkg_1.0.0_amd64.deb",
            "aur-sha512",
            "sha512",
        )

        with patch.object(sources, "resolve_candidate", return_value=candidate), patch.object(deb, "download") as download:
            resolved = cli.resolve_entry("pkg", entry, previous_entry)

        download.assert_not_called()
        self.assertEqual(resolved.entry, previous_entry)
        self.assertEqual(resolved.full_checked_arches, set())


class RefreshTests(unittest.TestCase):
    def test_refresh_preserves_lock_timestamp_when_packages_are_unchanged(self) -> None:
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
        previous_lock = {
            "version": 2,
            "generated_at": "2026-06-15T08:55:30+00:00",
            "packages": {"pkg": previous_entry},
        }
        config = {"packages": {"pkg": package_entry()}}
        resolved = cli.ResolvedEntry(
            previous_entry,
            set(),
            {"amd64": {"status": "ok", "source": "github", "update_policy": "track"}},
        )

        with (
            patch.object(cli, "load_config", return_value=config),
            patch.object(cli, "load_json", return_value=previous_lock),
            patch.object(cli, "resolve_entry", return_value=resolved),
            patch.object(cli, "worker_count", return_value=1),
            patch.object(health_module, "check_artifacts", return_value={"version": 2, "generated_at": "artifact-check", "packages": {}}),
            patch.object(cli, "write_json") as write_json,
        ):
            cli.refresh()

        self.assertEqual(
            write_json.mock_calls[0],
            call(cli.LOCK_PATH, previous_lock),
        )

    def test_refresh_updates_lock_timestamp_when_packages_change(self) -> None:
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
        updated_entry = {
            "homepage": "https://example.test/pkg",
            "architectures": {
                "amd64": {
                    "source": "github",
                    "update_policy": "track",
                    "resolved_at": "current",
                    "artifact": {
                        **locked_artifact(),
                        "url": "https://example.test/pkg-2.deb",
                        "upstream_version": "2.0.0",
                        "asset_name": "pkg_2.0.0_amd64.deb",
                        "filename": "pkg_2.0.0_amd64.deb",
                        "control": {"Package": "pkg", "Version": "2.0.0", "Architecture": "amd64"},
                        "size": 456,
                        "md5": "new-md5",
                        "sha1": "new-sha1",
                        "sha256": "new-sha256",
                    },
                }
            },
        }
        previous_lock = {
            "version": 2,
            "generated_at": "2026-06-15T08:55:30+00:00",
            "packages": {"pkg": previous_entry},
        }
        config = {"packages": {"pkg": package_entry()}}
        resolved = cli.ResolvedEntry(
            updated_entry,
            {"amd64"},
            {"amd64": {"status": "ok", "source": "github", "update_policy": "track"}},
        )

        with (
            patch.object(cli, "load_config", return_value=config),
            patch.object(cli, "load_json", return_value=previous_lock),
            patch.object(cli, "resolve_entry", return_value=resolved),
            patch.object(cli, "worker_count", return_value=1),
            patch.object(cli, "now_iso", return_value="2026-06-16T08:55:30+00:00"),
            patch.object(health_module, "check_artifacts", return_value={"version": 2, "generated_at": "artifact-check", "packages": {}}),
            patch.object(cli, "write_json") as write_json,
        ):
            cli.refresh()

        self.assertEqual(
            write_json.mock_calls[0],
            call(
                cli.LOCK_PATH,
                {
                    "version": 2,
                    "generated_at": "2026-06-16T08:55:30+00:00",
                    "packages": {"pkg": updated_entry},
                },
            ),
        )


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

        candidate = sources.resolve_candidate(
            architecture,
            fetch_json=cli.fetch_json,
            fetch_text=lambda url, headers=None: srcinfo,
            root=cli.ROOT,
        )

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

        candidate = sources.resolve_candidate(
            architecture,
            fetch_json=cli.fetch_json,
            fetch_text=lambda url, headers=None: srcinfo,
            root=cli.ROOT,
        )

        self.assertEqual(candidate.url, "https://example.test/example-arm64.deb")
        self.assertEqual(candidate.asset_name, "example.deb")
        self.assertEqual(candidate.expected_hash, "deb-sha256")
        self.assertEqual(candidate.hash_algorithm, "sha256")

    def test_sourceforge_regex_selects_matching_deb_and_sha1(self) -> None:
        html = """
<script>
net.sf.files = {"deadbeef-static_1.10.3~alpha-1_amd64.deb":{"name":"deadbeef-static_1.10.3~alpha-1_amd64.deb","download_url":"https://sourceforge.net/projects/deadbeef/files/Builds/master/linux/deadbeef-static_1.10.3~alpha-1_amd64.deb/download","downloadable":true,"sha1":"sha1-amd64","md5":"md5-amd64"},"notes.txt":{"name":"notes.txt","download_url":"https://example.test/notes.txt","downloadable":true,"sha1":"sha1-notes","md5":"md5-notes"}};
</script>
""".strip()
        architecture = {
            "update_policy": "track",
            "source": {
                "type": "sourceforge",
                "project": "deadbeef",
                "path": "Builds/master/linux",
                "asset_regex": r"deadbeef-static_.+_amd64\.deb",
            },
        }

        candidate = sources.resolve_candidate(
            architecture,
            fetch_json=cli.fetch_json,
            fetch_text=lambda url, headers=None: html,
            root=cli.ROOT,
        )

        self.assertEqual(
            candidate.url,
            "https://sourceforge.net/projects/deadbeef/files/Builds/master/linux/deadbeef-static_1.10.3~alpha-1_amd64.deb/download",
        )
        self.assertEqual(candidate.asset_name, "deadbeef-static_1.10.3~alpha-1_amd64.deb")
        self.assertEqual(candidate.upstream_version, "deadbeef-static_1.10.3~alpha-1_amd64.deb")
        self.assertEqual(candidate.expected_hash, "sha1-amd64")
        self.assertEqual(candidate.hash_algorithm, "sha1")


class ArtifactHealthTests(unittest.TestCase):
    def test_light_health_uses_head_and_compares_content_length(self) -> None:
        artifact = locked_artifact()

        def fake_urlopen(request: object, timeout: int = 0) -> FakeResponse:
            self.assertEqual(request.get_method(), "HEAD")
            self.assertEqual(timeout, 60)
            return FakeResponse({"Content-Length": "123"})

        with patch.object(health_module.urllib.request, "urlopen", side_effect=fake_urlopen):
            result = health_module.check_artifact_light(artifact, user_agent=cli.USER_AGENT)

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

        with patch.object(health_module.urllib.request, "urlopen", side_effect=fake_urlopen):
            result = health_module.check_artifact_light(artifact, user_agent=cli.USER_AGENT)

        self.assertEqual(calls, ["HEAD", "GET"])
        self.assertEqual(result, {"status": "ok", "check": "range", "size": 123})

    def test_range_health_ignores_partial_content_length_without_total_size(self) -> None:
        artifact = locked_artifact()

        def fake_urlopen(request: object, timeout: int = 0) -> FakeResponse:
            if request.get_method() == "HEAD":
                raise urllib.error.HTTPError(artifact["url"], 405, "Method Not Allowed", {}, None)
            return FakeResponse({"Content-Length": "1"}, status=206)

        with patch.object(health_module.urllib.request, "urlopen", side_effect=fake_urlopen):
            result = health_module.check_artifact_light(artifact, user_agent=cli.USER_AGENT)

        self.assertEqual(result, {"status": "ok", "check": "range"})

    def test_full_health_downloads_and_hashes_artifact(self) -> None:
        artifact = locked_artifact()
        with tempfile.NamedTemporaryFile() as tmp:
            path = Path(tmp.name)
            path.write_bytes(b"abc")
            artifact["size"] = 3
            artifact["sha256"] = "actual-sha256"
            with (
                patch.object(deb, "download", return_value=path) as download,
                patch.object(deb, "file_hash", return_value="actual-sha256") as file_hash,
            ):
                result = health_module.check_artifact(
                    artifact,
                    cache_dir=cli.CACHE_DIR,
                    user_agent=cli.USER_AGENT,
                )

        download.assert_called_once_with(
            "https://example.test/pkg.deb",
            cache_dir=cli.CACHE_DIR,
            user_agent=cli.USER_AGENT,
            expected_hash="actual-sha256",
        )
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

        with patch.object(health_module, "check_artifact", return_value={"status": "ok", "check": "full"}) as check_artifact:
            health = health_module.check_artifacts(
                lock,
                jobs=1,
                full_artifact_check=True,
                full_checked_artifacts=None,
                now_iso=cli.now_iso,
                worker_count=cli.worker_count,
                cache_dir=cli.CACHE_DIR,
                user_agent=cli.USER_AGENT,
            )

        check_artifact.assert_called_once()
        self.assertEqual(health["packages"]["pkg"]["artifacts"]["amd64"], {"status": "ok", "check": "full"})

    def test_check_artifacts_uses_light_check_without_cache_dir_failure(self) -> None:
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

        with patch.object(health_module, "fetch_artifact_size", return_value=123):
            health = health_module.check_artifacts(
                lock,
                jobs=1,
                full_artifact_check=False,
                full_checked_artifacts=None,
                now_iso=cli.now_iso,
                worker_count=cli.worker_count,
                cache_dir=cli.CACHE_DIR,
                user_agent=cli.USER_AGENT,
            )

        self.assertEqual(health["packages"]["pkg"]["artifacts"]["amd64"], {"status": "ok", "check": "head", "size": 123})


class BuildStateFileTests(unittest.TestCase):
    def test_copy_state_files_writes_missing_health_reports_as_deploy_artifacts(self) -> None:
        lock = {
            "packages": {
                "pkg": {
                    "architectures": {
                        "amd64": {
                            "artifact": locked_artifact(),
                        }
                    }
                }
            }
        }

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            dist = root / "dist"
            dist.mkdir()
            lock_path = root / "apt-index.lock.json"
            track_health_path = root / "track_health.json"
            artifact_health_path = root / "artifact_health.json"
            lock_path.write_text(json.dumps(lock), encoding="utf-8")

            site_data_module.copy_state_files(
                lock,
                track_health_path=track_health_path,
                artifact_health_path=artifact_health_path,
                dist_dir=dist,
                write_json=cli.write_json,
                now_iso=cli.now_iso,
            )

            self.assertFalse(dist.joinpath("apt-index.lock.json").exists())
            self.assertFalse(track_health_path.exists())
            self.assertFalse(artifact_health_path.exists())
            track_health = json.loads(dist.joinpath("track_health.json").read_text(encoding="utf-8"))
            artifact_health = json.loads(dist.joinpath("artifact_health.json").read_text(encoding="utf-8"))

        self.assertEqual(track_health["status"], "not_generated")
        self.assertEqual(track_health["packages"]["pkg"]["architectures"]["amd64"]["status"], "not_checked")
        self.assertEqual(artifact_health["status"], "not_generated")
        self.assertEqual(artifact_health["packages"]["pkg"]["artifacts"]["amd64"]["check"], "not_generated")

    def test_build_writes_site_data_without_publishing_lockfile(self) -> None:
        lock = {
            "generated_at": "2026-06-15T08:55:30+00:00",
            "packages": {
                "pkg": {
                    "homepage": "https://example.test/pkg",
                    "architectures": {
                        "amd64": {
                            "source": "github",
                            "update_policy": "track",
                            "artifact": locked_artifact(),
                        }
                    },
                }
            },
        }
        config = {
            "suite": "stable",
            "component": "main",
            "repository": {"base_url": "https://example.test"},
        }

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            dist = root / "dist"
            lock_path = root / "apt-index.lock.json"
            static_dir = root / "static"
            static_dir.mkdir()
            static_dir.joinpath("index.html").write_text(
                "__BASE_URL__ __SUITE__ __COMPONENT__ __PACKAGE_INDEX_LINKS__\n",
                encoding="utf-8",
            )
            lock_path.write_text(json.dumps(lock), encoding="utf-8")

            with (
                patch.object(cli, "LOCK_PATH", lock_path),
                patch.object(cli, "TRACK_HEALTH_PATH", root / "track_health.json"),
                patch.object(cli, "ARTIFACT_HEALTH_PATH", root / "artifact_health.json"),
                patch.object(cli, "DIST_DIR", dist),
                patch.object(cli, "STATIC_DIR", static_dir),
                patch.object(cli, "load_config", return_value=config),
                patch.object(cli, "ensure_signing_key", return_value="key"),
                patch.object(redirect, "write_redirect_rules"),
                patch.object(cli, "write_release"),
                patch.object(cli, "sign_release"),
            ):
                cli.build()

            self.assertFalse(dist.joinpath("apt-index.lock.json").exists())
            self.assertTrue(dist.joinpath("site-data.json").exists())
            site_data = json.loads(dist.joinpath("site-data.json").read_text(encoding="utf-8"))

        self.assertEqual(site_data["generated_at"], "2026-06-15T08:55:30+00:00")
        self.assertEqual(site_data["summary"]["artifact_count"], 1)
        self.assertEqual(site_data["summary"]["downloads_last_days"], 0)


class SiteDataTests(unittest.TestCase):
    def test_format_site_data_groups_artifacts_and_merges_health_and_downloads(self) -> None:
        lock = {
            "generated_at": "2026-06-15T08:55:30+00:00",
            "packages": {
                "pkg": {
                    "homepage": "https://example.test/pkg",
                    "architectures": {
                        "arm64": {
                            "source": "github",
                            "update_policy": "track",
                            "artifact": {
                                **locked_artifact(),
                                "filename": "pkg_1.0.0_arm64.deb",
                                "control": {"Package": "pkg", "Version": "1.0.0", "Architecture": "arm64"},
                            },
                        },
                        "amd64": {
                            "source": "github",
                            "update_policy": "track",
                            "artifact": locked_artifact(),
                        },
                    },
                }
            },
        }
        track_health = {
            "packages": {
                "pkg": {
                    "architectures": {
                        "amd64": {"status": "ok"},
                        "arm64": {"status": "kept_previous"},
                    }
                }
            }
        }
        artifact_health = {
            "packages": {
                "pkg": {
                    "artifacts": {
                        "amd64": {"status": "ok"},
                        "arm64": {"status": "ok"},
                    }
                }
            }
        }
        download_stats = {
            "window_days": 30,
            "packages": [
                {"entry_name": "pkg", "arch": "amd64", "downloads": 4, "last_7_days": 2},
                {"entry_name": "pkg", "arch": "arm64", "downloads": 7, "last_7_days": 3},
            ],
        }

        site_data = site_data_module.format_site_data(lock, track_health, artifact_health, download_stats)

        self.assertEqual(site_data["summary"]["entry_count"], 1)
        self.assertEqual(site_data["summary"]["row_count"], 1)
        self.assertEqual(site_data["summary"]["artifact_count"], 2)
        self.assertEqual(site_data["summary"]["downloads_last_days"], 11)
        self.assertEqual(site_data["summary"]["downloads_last_7_days"], 5)
        self.assertFalse(site_data["summary"]["all_healthy"])
        self.assertEqual(site_data["packages"][0]["package_name"], "pkg")
        self.assertEqual([item["arch"] for item in site_data["packages"][0]["artifacts"]], ["amd64", "arm64"])
        self.assertEqual(site_data["packages"][0]["artifacts"][1]["status_class"], "warn")

    def test_format_site_data_splits_rows_by_upstream_package_name_and_sorts_rows(self) -> None:
        lock = {
            "generated_at": "2026-06-15T08:55:30+00:00",
            "packages": {
                "bottom": {
                    "homepage": "https://example.test/bottom",
                    "architectures": {
                        "arm64": {
                            "source": "github",
                            "update_policy": "track",
                            "artifact": {
                                **locked_artifact(),
                                "filename": "bottom_1.0.0_arm64.deb",
                                "control": {"Package": "bottom-arm64", "Version": "1.0.0", "Architecture": "arm64"},
                            },
                        },
                        "amd64": {
                            "source": "github",
                            "update_policy": "track",
                            "artifact": {
                                **locked_artifact(),
                                "filename": "bottom_1.0.0_amd64.deb",
                                "control": {"Package": "bottom", "Version": "1.0.0", "Architecture": "amd64"},
                            },
                        },
                    },
                }
            },
        }

        site_data = site_data_module.format_site_data(
            lock,
            {"packages": {}},
            {"packages": {}},
            download_stats.empty_download_stats("not_generated", 30, cli.now_iso),
        )

        self.assertEqual(site_data["summary"]["row_count"], 2)
        self.assertEqual([row["package_name"] for row in site_data["packages"]], ["bottom", "bottom-arm64"])

    def test_write_site_data_uses_fallback_reports_and_empty_downloads(self) -> None:
        lock = {
            "generated_at": "2026-06-15T08:55:30+00:00",
            "packages": {
                "pkg": {
                    "homepage": "https://example.test/pkg",
                    "architectures": {
                        "amd64": {
                            "source": "github",
                            "update_policy": "track",
                            "artifact": locked_artifact(),
                        }
                    },
                }
            },
        }

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            output = root / "nested" / "site-data.json"
            lock_path = root / "apt-index.lock.json"
            lock_path.write_text(json.dumps(lock), encoding="utf-8")

            site_data_module.write_site_data(
                output,
                root / "missing-download-stats.json",
                lock_path=lock_path,
                track_health_path=root / "track_health.json",
                artifact_health_path=root / "artifact_health.json",
                load_json=cli.load_json,
                write_json=cli.write_json,
                empty_download_stats=lambda reason: download_stats.empty_download_stats(reason, 30, cli.now_iso),
                now_iso=cli.now_iso,
            )

            site_data = json.loads(output.read_text(encoding="utf-8"))

        artifact = site_data["packages"][0]["artifacts"][0]
        self.assertEqual(artifact["track_status"], "not_checked")
        self.assertEqual(artifact["artifact_status"], "not_checked")
        self.assertEqual(artifact["downloads"], 0)
        self.assertEqual(site_data["window_days"], 30)


class RedirectRulesTests(unittest.TestCase):
    def test_write_redirect_rules_writes_entry_shards_and_snapshot(self) -> None:
        lock = {
            "packages": {
                "pkg": {
                    "architectures": {
                        "amd64": {"artifact": locked_artifact()},
                    }
                }
            }
        }

        with tempfile.TemporaryDirectory() as tmp:
            dist = Path(tmp) / "dist"
            dist.mkdir()
            redirects = redirect.write_redirect_rules(
                lock,
                "main",
                dist_dir=dist,
                redirect_rules_dirname=cli.REDIRECT_RULES_DIRNAME,
                redirect_snapshot_filename=cli.REDIRECT_SNAPSHOT_FILENAME,
                write_json=cli.write_json,
                package_virtual_path=cli.package_virtual_path,
            )
            shard = json.loads(dist.joinpath("redirect-rules", "main", "pkg.json").read_text(encoding="utf-8"))
            snapshot = redirect.read_redirect_snapshot(dist / "redirect-rules" / "snapshot.json.zst")

        self.assertEqual(shard, {"pkg_1.0.0_amd64.deb": "https://example.test/pkg.deb"})
        self.assertEqual(redirects, {"/pool/main/pkg/pkg_1.0.0_amd64.deb": "https://example.test/pkg.deb"})
        self.assertEqual(snapshot, redirects)

    def test_plan_redirect_purge_outputs_changed_and_removed_urls(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            snapshot = root / "snapshot.json.zst"
            output = root / "purge.txt"
            redirect.write_redirect_snapshot(
                snapshot,
                {
                    "/pool/main/pkg/new.deb": "https://example.test/new.deb",
                    "/pool/main/pkg/unchanged.deb": "https://example.test/unchanged.deb",
                },
            )

            urls = redirect.plan_redirect_purge(
                output,
                snapshot,
                "https://deb.example.test",
                redirect_rules_dirname=cli.REDIRECT_RULES_DIRNAME,
                redirect_snapshot_filename=cli.REDIRECT_SNAPSHOT_FILENAME,
                legacy_redirect_rules_paths=cli.LEGACY_REDIRECT_RULES_PATHS,
                fetch_previous_redirect_snapshot=lambda base_url, strict: {
                    "/pool/main/pkg/old.deb": "https://example.test/old.deb",
                    "/pool/main/pkg/new.deb": "https://example.test/old-target.deb",
                    "/pool/main/pkg/unchanged.deb": "https://example.test/unchanged.deb",
                },
            )

            lines = output.read_text(encoding="utf-8").splitlines()

        self.assertEqual(
            urls,
            [
                "https://deb.example.test/pool/main/pkg/new.deb",
                "https://deb.example.test/pool/main/pkg/old.deb",
                "https://deb.example.test/redirect-rules/main/pkg.json",
                "https://deb.example.test/redirect-rules/snapshot.json.zst",
                "https://deb.example.test/redirect_rules.json",
            ],
        )
        self.assertEqual(lines, urls)

    def test_plan_redirect_purge_includes_added_package_paths_for_negative_cache_invalidation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            snapshot = root / "snapshot.json.zst"
            output = root / "purge.txt"
            redirect.write_redirect_snapshot(
                snapshot,
                {
                    "/pool/main/pkg/new-only.deb": "https://example.test/new-only.deb",
                },
            )

            urls = redirect.plan_redirect_purge(
                output,
                snapshot,
                "https://deb.example.test",
                redirect_rules_dirname=cli.REDIRECT_RULES_DIRNAME,
                redirect_snapshot_filename=cli.REDIRECT_SNAPSHOT_FILENAME,
                legacy_redirect_rules_paths=cli.LEGACY_REDIRECT_RULES_PATHS,
                fetch_previous_redirect_snapshot=lambda base_url, strict: {},
            )

        self.assertEqual(
            urls,
            [
                "https://deb.example.test/pool/main/pkg/new-only.deb",
                "https://deb.example.test/redirect-rules/main/pkg.json",
                "https://deb.example.test/redirect-rules/snapshot.json.zst",
                "https://deb.example.test/redirect_rules.json",
            ],
        )

    def test_fetch_previous_redirect_snapshot_tolerates_invalid_first_deploy_asset(self) -> None:
        redirects = redirect.fetch_previous_redirect_snapshot(
            "https://deb.example.test",
            redirect_rules_dirname=cli.REDIRECT_RULES_DIRNAME,
            redirect_snapshot_filename=cli.REDIRECT_SNAPSHOT_FILENAME,
            fetch_bytes=lambda url, headers=None: b"not zstd",
        )

        self.assertEqual(redirects, {})

    def test_purge_redirect_cache_skips_purge_errors_by_default(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            urls = Path(tmp) / "urls.txt"
            urls.write_text("https://deb.example.test/redirect_rules.json\n", encoding="utf-8")

            with patch.dict(cli.os.environ, {"CLOUDFLARE_API_TOKEN": "token"}, clear=True):
                redirect.purge_redirect_cache(
                    urls,
                    redirect_rules_dirname=cli.REDIRECT_RULES_DIRNAME,
                    resolve_cloudflare_zone_id=lambda token, hostname: "zone",
                    purge_cloudflare_urls=Mock(side_effect=RuntimeError("Authentication error")),
                    purge_cloudflare_prefixes=Mock(),
                )

    def test_purge_redirect_cache_also_purges_redirect_rules_prefix(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            urls = Path(tmp) / "urls.txt"
            urls.write_text("https://deb.example.test/redirect_rules.json\n", encoding="utf-8")

            purge_urls = Mock()
            purge_prefixes = Mock()
            with patch.dict(cli.os.environ, {"CLOUDFLARE_API_TOKEN": "token"}, clear=True):
                redirect.purge_redirect_cache(
                    urls,
                    redirect_rules_dirname=cli.REDIRECT_RULES_DIRNAME,
                    resolve_cloudflare_zone_id=lambda token, hostname: "zone",
                    purge_cloudflare_urls=purge_urls,
                    purge_cloudflare_prefixes=purge_prefixes,
                )

        purge_urls.assert_called_once_with("zone", "token", ["https://deb.example.test/redirect_rules.json"])
        purge_prefixes.assert_called_once_with("zone", "token", ["deb.example.test/redirect-rules"])

    def test_purge_redirect_cache_strict_raises_purge_errors(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            urls = Path(tmp) / "urls.txt"
            urls.write_text("https://deb.example.test/redirect_rules.json\n", encoding="utf-8")

            with (
                patch.dict(cli.os.environ, {"CLOUDFLARE_API_TOKEN": "token"}, clear=True),
                self.assertRaisesRegex(RuntimeError, "Authentication error"),
            ):
                redirect.purge_redirect_cache(
                    urls,
                    redirect_rules_dirname=cli.REDIRECT_RULES_DIRNAME,
                    resolve_cloudflare_zone_id=lambda token, hostname: "zone",
                    purge_cloudflare_urls=Mock(side_effect=RuntimeError("Authentication error")),
                    purge_cloudflare_prefixes=Mock(),
                    strict=True,
                )


class DownloadStatsTests(unittest.TestCase):
    def test_formats_download_stats_for_public_json(self) -> None:
        stats = download_stats.format_download_stats(
            [{"entry_name": "bat", "arch": "amd64", "downloads": 12}],
            [{"entry_name": "bat", "arch": "amd64", "downloads": 3}],
            [{"day": "2026-06-14T00:00:00Z", "downloads": 5}],
            "deb.example.test",
            30,
            cli.now_iso,
        )

        self.assertEqual(stats["source"], "cloudflare_http_requests")
        self.assertEqual(stats["hostname"], "deb.example.test")
        self.assertEqual(stats["totals"], {"downloads": 12, "last_days": 12, "last_7_days": 3})
        self.assertEqual(stats["packages"], [{"entry_name": "bat", "arch": "amd64", "downloads": 12, "last_7_days": 3}])
        self.assertEqual(stats["daily"], [{"date": "2026-06-14", "downloads": 5}])

    def test_aggregates_cloudflare_path_rows_by_entry_and_arch(self) -> None:
        path_index = {
            "/pool/main/bat/bat_1.0_amd64.deb": ("bat", "amd64"),
        }
        rows = download_stats.aggregate_path_download_rows(
            [
                {"count": 4, "dimensions": {"clientRequestPath": "/pool/main/bat/bat_1.0_amd64.deb"}},
                {"count": 2, "dimensions": {"clientRequestPath": "/pool/main/bat/bat_1.0_amd64.deb"}},
                {"count": 1, "dimensions": {"clientRequestPath": "/dists/stable/InRelease"}},
            ],
            path_index,
        )

        self.assertEqual(rows, [{"entry_name": "bat", "arch": "amd64", "downloads": 6}])

    def test_package_download_path_index_uses_lockfile_control_architecture(self) -> None:
        lock = {
            "packages": {
                "fastfetch": {
                    "architectures": {
                        "amd64": {
                            "artifact": {
                                "filename": "fastfetch-linux-amd64.deb",
                                "control": {"Architecture": "amd64"},
                            }
                        }
                    }
                }
            }
        }

        index = download_stats.package_download_path_index(lock, "main", cli.package_virtual_path)

        self.assertEqual(index, {"/pool/main/fastfetch/fastfetch-linux-amd64.deb": ("fastfetch", "amd64")})

    def test_fetch_download_stats_splits_cloudflare_queries_into_daily_windows(self) -> None:
        calls: list[dict[str, object]] = []
        path_index = {
            "/pool/main/bat/bat_1.0_amd64.deb": ("bat", "amd64"),
            "/pool/main/bat/bat_1.0_arm64.deb": ("bat", "arm64"),
        }
        rows_by_call = [
            [{"count": 1, "dimensions": {"clientRequestPath": "/pool/main/bat/bat_1.0_amd64.deb"}}],
            [{"count": 2, "dimensions": {"clientRequestPath": "/pool/main/bat/bat_1.0_arm64.deb"}}],
        ]

        def fake_graphql(token: str, query: str, variables: dict[str, object]) -> dict[str, object]:
            calls.append(variables)
            return {
                "data": {
                    "viewer": {
                        "zones": [
                            {
                                "packageRows": rows_by_call[len(calls) - 1],
                            }
                        ]
                    }
                }
            }

        stats = download_stats.fetch_download_stats(
            "zone",
            "token",
            "deb.example.test",
            days=2,
            path_index=path_index,
            max_days=cli.CLOUDFLARE_HTTP_ANALYTICS_MAX_DAYS,
            cloudflare_graphql=fake_graphql,
            now=lambda: datetime(2026, 6, 15, tzinfo=timezone.utc),
            now_iso=cli.now_iso,
            graphql_time=cli.graphql_time,
        )

        self.assertEqual(len(calls), 2)
        for variables in calls:
            package_filter = variables["packageFilter"]
            start = datetime.fromisoformat(package_filter["datetime_geq"])
            end = datetime.fromisoformat(package_filter["datetime_lt"])
            self.assertLessEqual((end - start).total_seconds(), 24 * 60 * 60)
        self.assertEqual(stats["totals"], {"downloads": 3, "last_days": 3, "last_7_days": 3})
        self.assertEqual(
            stats["packages"],
            [
                {"entry_name": "bat", "arch": "arm64", "downloads": 2, "last_7_days": 2},
                {"entry_name": "bat", "arch": "amd64", "downloads": 1, "last_7_days": 1},
            ],
        )
        self.assertEqual([row["downloads"] for row in stats["daily"]], [1, 2])

    def test_fetch_download_stats_caps_window_to_cloudflare_retention(self) -> None:
        calls: list[dict[str, object]] = []

        def fake_graphql(token: str, query: str, variables: dict[str, object]) -> dict[str, object]:
            calls.append(variables)
            return {"data": {"viewer": {"zones": [{"packageRows": []}]}}}

        stats = download_stats.fetch_download_stats(
            "zone",
            "token",
            "deb.example.test",
            days=30,
            path_index={},
            max_days=cli.CLOUDFLARE_HTTP_ANALYTICS_MAX_DAYS,
            cloudflare_graphql=fake_graphql,
            now=lambda: datetime(2026, 6, 15, tzinfo=timezone.utc),
            now_iso=cli.now_iso,
            graphql_time=cli.graphql_time,
        )

        self.assertEqual(len(calls), cli.CLOUDFLARE_HTTP_ANALYTICS_MAX_DAYS)
        self.assertEqual(stats["window_days"], cli.CLOUDFLARE_HTTP_ANALYTICS_MAX_DAYS)

    def test_write_download_stats_writes_empty_file_without_cloudflare_credentials(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, patch.dict(cli.os.environ, {}, clear=True):
            output = Path(tmp) / "nested" / "download_stats.json"
            download_stats.write_download_stats(
                output,
                "deb.example.test",
                days=14,
                strict=False,
                load_json=cli.load_json,
                load_config=cli.load_config,
                write_json=cli.write_json,
                empty_download_stats=lambda reason, days: download_stats.empty_download_stats(reason, days, cli.now_iso),
                resolve_cloudflare_zone_id=lambda token, hostname: "zone",
                fetch_download_stats=Mock(),
                lock_path=cli.LOCK_PATH,
                package_virtual_path=cli.package_virtual_path,
            )

            stats = cli.load_json(output, None)

        self.assertEqual(stats["source"], "none")
        self.assertEqual(stats["reason"], "missing_cloudflare_credentials")
        self.assertEqual(stats["window_days"], 14)

    def test_write_download_stats_writes_empty_file_when_query_fails(self) -> None:
        env = {"CLOUDFLARE_ZONE_ID": "zone", "CLOUDFLARE_API_TOKEN": "token"}
        with (
            tempfile.TemporaryDirectory() as tmp,
            patch.dict(cli.os.environ, env, clear=True),
        ):
            output = Path(tmp) / "download_stats.json"
            download_stats.write_download_stats(
                output,
                "deb.example.test",
                days=30,
                strict=False,
                load_json=cli.load_json,
                load_config=lambda: {"component": "main"},
                write_json=cli.write_json,
                empty_download_stats=lambda reason, days: download_stats.empty_download_stats(reason, days, cli.now_iso),
                resolve_cloudflare_zone_id=lambda token, hostname: "zone",
                fetch_download_stats=Mock(side_effect=RuntimeError("dataset not found")),
                lock_path=Path(tmp) / "missing-lock.json",
                package_virtual_path=cli.package_virtual_path,
            )

            stats = cli.load_json(output, None)

        self.assertEqual(stats["source"], "none")
        self.assertEqual(stats["reason"], "analytics_query_failed")

    def test_resolves_cloudflare_zone_id_from_hostname(self) -> None:
        payloads = {
            "https://api.cloudflare.com/client/v4/zones?name=deb.example.test": {"success": True, "result": []},
            "https://api.cloudflare.com/client/v4/zones?name=example.test": {
                "success": True,
                "result": [{"id": "zone-id", "name": "example.test"}],
            },
        }

        with (
            patch.dict(cli.os.environ, {}, clear=True),
        ):
            zone_id = redirect.resolve_cloudflare_zone_id(
                "token",
                "deb.example.test",
                fetch_json=lambda url, headers=None: payloads[url],
            )

        self.assertEqual(zone_id, "zone-id")

    def test_write_download_stats_resolves_zone_from_hostname(self) -> None:
        env = {"CLOUDFLARE_API_TOKEN": "token"}
        with (
            tempfile.TemporaryDirectory() as tmp,
            patch.dict(cli.os.environ, env, clear=True),
        ):
            output = Path(tmp) / "download_stats.json"
            resolve_zone = Mock(return_value="zone")
            fetch_stats = Mock(return_value={"source": "cloudflare_http_requests"})
            download_stats.write_download_stats(
                output,
                "deb.example.test",
                days=30,
                strict=False,
                load_json=cli.load_json,
                load_config=lambda: {"component": "main"},
                write_json=cli.write_json,
                empty_download_stats=lambda reason, days: download_stats.empty_download_stats(reason, days, cli.now_iso),
                resolve_cloudflare_zone_id=resolve_zone,
                fetch_download_stats=fetch_stats,
                lock_path=Path(tmp) / "missing-lock.json",
                package_virtual_path=cli.package_virtual_path,
            )

            stats = cli.load_json(output, None)

        resolve_zone.assert_called_once_with("token", "deb.example.test")
        self.assertEqual(stats, {"source": "cloudflare_http_requests"})


class WorkerGenerationTests(unittest.TestCase):
    def test_write_headers_sets_long_edge_ttl_for_redirect_rules_assets(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "_headers"
            publish.write_headers(
                path,
                site_data_filename=cli.SITE_DATA_FILENAME,
                site_data_browser_ttl_policy=cli.SITE_DATA_BROWSER_TTL_POLICY,
                site_data_cdn_ttl_policy=cli.SITE_DATA_CDN_TTL_POLICY,
                redirect_rules_dirname=cli.REDIRECT_RULES_DIRNAME,
                static_redirect_rules_browser_ttl_policy=cli.STATIC_REDIRECT_RULES_BROWSER_TTL_POLICY,
                static_redirect_rules_cdn_ttl_policy=cli.STATIC_REDIRECT_RULES_CDN_TTL_POLICY,
            )

            headers = path.read_text(encoding="utf-8")

        self.assertIn("/site-data.json", headers)
        self.assertIn("Cache-Control: public, max-age=300, must-revalidate", headers)
        self.assertIn("Cloudflare-CDN-Cache-Control: public, max-age=300", headers)
        self.assertIn("/redirect-rules/*.json.zst", headers)
        self.assertIn("Cache-Control: public, max-age=0, must-revalidate", headers)
        self.assertIn("Cloudflare-CDN-Cache-Control: public, max-age=31536000, stale-while-revalidate=86400, stale-if-error=604800", headers)
        self.assertIn("Content-Encoding: zstd", headers)
        self.assertIn("/redirect-rules/*.json", headers)

    def test_worker_reads_redirect_shard_and_caches_redirect_response(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "_worker.js"
            publish.write_worker(path, cli.WORKER_SCRIPT_PATH)

            worker = path.read_text(encoding="utf-8")

        self.assertIn("const cache = caches.default", worker)
        self.assertIn("const cacheGetResponse = (response) => {", worker)
        self.assertIn("await cache.match(cacheKey)", worker)
        self.assertIn("ctx.waitUntil(cache.put(cacheKey, redirectResponse.clone())", worker)
        self.assertIn("if (!rulesResponse || !rulesResponse.ok) {", worker)
        self.assertIn('console.warn("redirect shard fetch failed", error);', worker)
        self.assertIn('const notFound = () => new Response("package redirect not found"', worker)
        self.assertIn('"Cache-Control": "public, max-age=60, s-maxage=60"', worker)
        self.assertIn('"Cloudflare-CDN-Cache-Control": "public, max-age=60"', worker)
        self.assertIn("/redirect-rules/${component}/${entryName}.json", worker)
        self.assertIn("const target = rules[filename]", worker)
        self.assertIn('"Cache-Control": "public, max-age=300, s-maxage=2592000"', worker)
        self.assertIn('"Cloudflare-CDN-Cache-Control": "public, max-age=2592000"', worker)
        self.assertIn("status: 302", worker)
        self.assertNotIn("env.DOWNLOADS", worker)


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
