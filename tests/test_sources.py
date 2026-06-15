from __future__ import annotations

import unittest
from pathlib import Path
from unittest.mock import patch

from apt_index import sources
from apt_index.config import AurArchSource, EntryArchitecture, GithubArchSource, ScriptArchSource, SourceforgeArchSource, UrlArchSource
from apt_index.sources import github as github_sources


class ResolverFetchers:
    def __init__(self, *, json_payload: dict[str, object] | None = None, text_payload: str = "") -> None:
        self.json_payload = json_payload or {}
        self.text_payload = text_payload
        self.json_calls: list[tuple[str, dict[str, str] | None]] = []

    def fetch_json(self, url: str, headers: dict[str, str] | None = None) -> dict[str, object]:
        self.json_calls.append((url, headers))
        return self.json_payload

    def fetch_text(self, url: str, headers: dict[str, str] | None = None) -> str:
        del url, headers
        return self.text_payload


class ResolveCandidateTests(unittest.TestCase):
    def test_github_fixed_release_selects_matching_asset(self) -> None:
        architecture = EntryArchitecture(
            update_policy="fixed",
            source=GithubArchSource(
                repo="example/pkg",
                asset_pattern="pkg_*_amd64.deb",
                release_tag="v1.2.3",
            ),
        )
        fetchers = ResolverFetchers(
            json_payload={
                "tag_name": "v1.2.3",
                "assets": [
                    {"name": "pkg_1.2.3_amd64.deb", "browser_download_url": "https://example.test/pkg_1.2.3_amd64.deb"},
                ],
            }
        )
        resolver = sources.build_candidate_resolver(
            fetch_json=fetchers.fetch_json,
            fetch_text=fetchers.fetch_text,
            root=Path("/tmp"),
        )

        with patch.dict(github_sources.os.environ, {}, clear=True), patch.object(github_sources.shutil, "which", return_value=None):
            candidate = resolver(architecture)

        self.assertEqual(
            fetchers.json_calls,
            [("https://api.github.com/repos/example/pkg/releases/tags/v1.2.3", {})],
        )
        self.assertEqual(candidate.url, "https://example.test/pkg_1.2.3_amd64.deb")
        self.assertEqual(candidate.upstream_version, "v1.2.3")
        self.assertEqual(candidate.asset_name, "pkg_1.2.3_amd64.deb")

    def test_github_latest_release_selects_matching_asset(self) -> None:
        architecture = EntryArchitecture(
            update_policy="track",
            source=GithubArchSource(
                repo="example/pkg",
                asset_pattern="pkg_*_arm64.deb",
            ),
        )
        fetchers = ResolverFetchers(
            json_payload={
                "tag_name": "v2.0.0",
                "assets": [
                    {"name": "pkg_2.0.0_amd64.deb", "browser_download_url": "https://example.test/pkg_2.0.0_amd64.deb"},
                    {"name": "pkg_2.0.0_arm64.deb", "browser_download_url": "https://example.test/pkg_2.0.0_arm64.deb"},
                ],
            }
        )
        resolver = sources.build_candidate_resolver(
            fetch_json=fetchers.fetch_json,
            fetch_text=fetchers.fetch_text,
            root=Path("/tmp"),
        )

        with patch.dict(github_sources.os.environ, {}, clear=True), patch.object(github_sources.shutil, "which", return_value=None):
            candidate = resolver(architecture)

        self.assertEqual(
            fetchers.json_calls,
            [("https://api.github.com/repos/example/pkg/releases/latest", {})],
        )
        self.assertEqual(candidate.url, "https://example.test/pkg_2.0.0_arm64.deb")
        self.assertEqual(candidate.upstream_version, "v2.0.0")
        self.assertEqual(candidate.asset_name, "pkg_2.0.0_arm64.deb")

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
        architecture = EntryArchitecture(
            update_policy="track",
            source=AurArchSource(
                package="google-chrome",
                asset_pattern="google-chrome-stable_*_amd64.deb",
            ),
        )
        fetchers = ResolverFetchers(text_payload=srcinfo)
        resolver = sources.build_candidate_resolver(
            fetch_json=fetchers.fetch_json,
            fetch_text=fetchers.fetch_text,
            root=Path("/tmp"),
        )

        candidate = resolver(architecture)

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
        architecture = EntryArchitecture(
            update_policy="track",
            source=AurArchSource(
                package="example-bin",
                asset_pattern="example.deb",
            ),
        )
        fetchers = ResolverFetchers(text_payload=srcinfo)
        resolver = sources.build_candidate_resolver(
            fetch_json=fetchers.fetch_json,
            fetch_text=fetchers.fetch_text,
            root=Path("/tmp"),
        )

        candidate = resolver(architecture)

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
        architecture = EntryArchitecture(
            update_policy="track",
            source=SourceforgeArchSource(
                project="deadbeef",
                path="Builds/master/linux",
                asset_regex=r"deadbeef-static_.+_amd64\.deb",
            ),
        )
        fetchers = ResolverFetchers(text_payload=html)
        resolver = sources.build_candidate_resolver(
            fetch_json=fetchers.fetch_json,
            fetch_text=fetchers.fetch_text,
            root=Path("/tmp"),
        )

        candidate = resolver(architecture)

        self.assertEqual(
            candidate.url,
            "https://sourceforge.net/projects/deadbeef/files/Builds/master/linux/deadbeef-static_1.10.3~alpha-1_amd64.deb/download",
        )
        self.assertEqual(candidate.asset_name, "deadbeef-static_1.10.3~alpha-1_amd64.deb")
        self.assertEqual(candidate.upstream_version, "deadbeef-static_1.10.3~alpha-1_amd64.deb")
        self.assertEqual(candidate.expected_hash, "sha1-amd64")
        self.assertEqual(candidate.hash_algorithm, "sha1")

    def test_url_source_returns_fixed_candidate(self) -> None:
        architecture = EntryArchitecture(
            update_policy="fixed",
            source=UrlArchSource(url="https://example.test/pkg_1.2.3_amd64.deb"),
        )
        fetchers = ResolverFetchers()
        resolver = sources.build_candidate_resolver(
            fetch_json=fetchers.fetch_json,
            fetch_text=fetchers.fetch_text,
            root=Path("/tmp"),
        )

        candidate = resolver(architecture)

        self.assertEqual(candidate.url, "https://example.test/pkg_1.2.3_amd64.deb")
        self.assertEqual(candidate.upstream_version, "fixed")
        self.assertEqual(candidate.asset_name, "pkg_1.2.3_amd64.deb")

    def test_script_source_is_unsupported(self) -> None:
        architecture = EntryArchitecture(
            update_policy="track",
            source=ScriptArchSource(command="./resolve.sh"),
        )
        fetchers = ResolverFetchers()
        resolver = sources.build_candidate_resolver(
            fetch_json=fetchers.fetch_json,
            fetch_text=fetchers.fetch_text,
            root=Path("/tmp"),
        )

        with self.assertRaisesRegex(RuntimeError, "unsupported source resolver 'script'"):
            resolver(architecture)


if __name__ == "__main__":
    unittest.main()
