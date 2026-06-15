from __future__ import annotations

import base64
import email.utils
import gzip
import hashlib
import html as html_module
import os
import shutil
from datetime import timezone
from pathlib import Path
from typing import Any, MutableMapping

import gpg
from loguru import logger
from pydantic import BaseModel, ConfigDict, Field

from apt_index import deb, publish, redirect, site_data as site_data_module
from apt_index.download_stats import DownloadStats
from apt_index.paths import ARTIFACT_HEALTH_PATH, DIST_DIR, ENV_PATH, GNUPG_DIR, STATIC_DIR, TRACK_HEALTH_PATH, WORKER_SCRIPT_PATH
from apt_index.published_state import PublishedState
from apt_index.runtime import JsonFiles, SystemClock

DOWNLOAD_STATS_FILENAME = "download_stats.json"
SITE_DATA_FILENAME = "site-data.json"
REDIRECT_RULES_DIRNAME = "redirect-rules"
REDIRECT_SNAPSHOT_FILENAME = "snapshot.json.zst"
SITE_DATA_TTL_SECONDS = 60 * 5
STATIC_REDIRECT_RULES_EDGE_TTL_SECONDS = 60 * 60 * 24 * 365
STATIC_REDIRECT_RULES_BROWSER_TTL_POLICY = "public, max-age=0, must-revalidate"
STATIC_REDIRECT_RULES_CDN_TTL_POLICY = (
    f"public, max-age={STATIC_REDIRECT_RULES_EDGE_TTL_SECONDS}, stale-while-revalidate=86400, stale-if-error=604800"
)
SITE_DATA_BROWSER_TTL_POLICY = f"public, max-age={SITE_DATA_TTL_SECONDS}, must-revalidate"
SITE_DATA_CDN_TTL_POLICY = f"public, max-age={SITE_DATA_TTL_SECONDS}"
STATIC_ASSET_FILENAMES = ("logo.webp",)
SIGNING_PRIVATE_KEY_ENV = "APT_INDEX_GPG_PRIVATE_KEY"
SIGNING_PRIVATE_KEY_B64_ENV = "APT_INDEX_GPG_PRIVATE_KEY_B64"
SIGNING_PASSPHRASE_ENV = "APT_INDEX_GPG_PASSPHRASE"


class DeployRepository(BaseModel):
    model_config = ConfigDict(frozen=True)

    origin: str = ""
    label: str = ""
    description: str = ""
    base_url: str


class DeploySigning(BaseModel):
    model_config = ConfigDict(frozen=True)

    key_name: str


class DeployConfig(BaseModel):
    model_config = ConfigDict(frozen=True)

    suite: str = ""
    component: str = ""
    repository: DeployRepository
    signing: DeploySigning | None = None

    @property
    def signing_key_name(self) -> str:
        if not self.signing:
            raise RuntimeError("signing.key_name is required")
        return self.signing.key_name


class DeployPaths(BaseModel):
    model_config = ConfigDict(frozen=True)

    dist_dir: Path = DIST_DIR
    track_health_path: Path = TRACK_HEALTH_PATH
    artifact_health_path: Path = ARTIFACT_HEALTH_PATH
    static_dir: Path = STATIC_DIR
    worker_script_path: Path = WORKER_SCRIPT_PATH
    gnupg_dir: Path = GNUPG_DIR
    env_path: Path = ENV_PATH


class RepositorySigner:
    def __init__(
        self,
        *,
        gnupg_dir: Path = GNUPG_DIR,
        env_path: Path = ENV_PATH,
        environ: MutableMapping[str, str] | None = None,
    ) -> None:
        self.gnupg_dir = gnupg_dir
        self.env_path = env_path
        self.environ = environ if environ is not None else os.environ
        self._dotenv_loaded = False

    def public_key(self, config: DeployConfig) -> str:
        self._load_dotenv()
        key_name = config.signing_key_name
        if not self._has_secret_key(key_name):
            self._import_private_key_from_env()
        if not self._has_secret_key(key_name):
            raise RuntimeError(f"imported signing key does not contain secret key for {key_name!r}")
        public_key = self._context(armor=True).key_export(pattern=key_name)
        if isinstance(public_key, bytes):
            return public_key.decode("utf-8")
        return str(public_key)

    def sign_release(self, config: DeployConfig, *, release_path: Path) -> None:
        self.public_key(config)
        key_name = config.signing_key_name
        key = self._signing_key(key_name)
        data = release_path.read_bytes()
        sign_mode = gpg.constants.sig.mode
        release_path.parent.joinpath("InRelease").write_bytes(self._sign(data, key=key, mode=sign_mode.CLEAR))
        release_path.parent.joinpath("Release.gpg").write_bytes(self._sign(data, key=key, mode=sign_mode.DETACH))

    def _load_dotenv(self) -> None:
        if self._dotenv_loaded:
            return
        self._dotenv_loaded = True
        if not self.env_path.exists():
            return

        for raw_line in self.env_path.read_text(encoding="utf-8").splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            if line.startswith("export "):
                line = line.removeprefix("export ").strip()
            name, value = line.split("=", 1)
            name = name.strip()
            value = value.strip()
            if not name or name in self.environ:
                continue
            if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
                value = value[1:-1]
            self.environ[name] = value.replace("\\n", "\n")

    def _private_key_material(self) -> str:
        key_material = self.environ.get(SIGNING_PRIVATE_KEY_ENV)
        key_material_b64 = self.environ.get(SIGNING_PRIVATE_KEY_B64_ENV)
        if not key_material and key_material_b64:
            key_material = base64.b64decode(key_material_b64).decode("utf-8")
        if not key_material:
            raise RuntimeError(
                "missing signing private key; set "
                f"{SIGNING_PRIVATE_KEY_B64_ENV} or {SIGNING_PRIVATE_KEY_ENV} before running apt-index build"
            )
        return key_material

    def _context(self, *, armor: bool = True, signers: list[Any] | None = None) -> Any:
        self.gnupg_dir.mkdir(mode=0o700, exist_ok=True)
        context_kwargs: dict[str, Any] = {"armor": armor, "home_dir": str(self.gnupg_dir)}
        if signers is not None:
            context_kwargs["signers"] = signers
        pinentry_mode = getattr(gpg.constants, "PINENTRY_MODE_LOOPBACK", None)
        if pinentry_mode is not None:
            context_kwargs["pinentry_mode"] = pinentry_mode
        return gpg.Context(**context_kwargs)

    def _matching_secret_keys(self, key_name: str) -> list[Any]:
        return list(self._context().keylist(pattern=key_name, secret=True))

    def _has_secret_key(self, key_name: str) -> bool:
        return bool(self._matching_secret_keys(key_name))

    def _import_private_key_from_env(self) -> None:
        self._context().op_import(self._private_key_material().encode("utf-8"))

    def _signing_key(self, key_name: str) -> Any:
        keys = self._matching_secret_keys(key_name)
        if not keys:
            raise RuntimeError(f"signing key {key_name!r} is not available")
        return keys[0]

    def _sign(self, data: bytes, *, key: Any, mode: Any) -> bytes:
        passphrase = self.environ.get(SIGNING_PASSPHRASE_ENV)
        context = self._context(armor=True, signers=[key])
        if passphrase:
            def passphrase_callback(hint: str, desc: str, prev_bad: bool, hook: object = None) -> str:
                del hint, desc, prev_bad, hook
                return passphrase

            context.set_passphrase_cb(passphrase_callback)
        signed_data, _ = context.sign(data, mode=mode)
        return signed_data


class DeployableAptTree(BaseModel):
    model_config = ConfigDict(frozen=True, arbitrary_types_allowed=True)

    config: DeployConfig
    state: PublishedState
    paths: DeployPaths = Field(default_factory=DeployPaths)
    clock: SystemClock = Field(default_factory=SystemClock)
    json_files: JsonFiles = Field(default_factory=JsonFiles)
    signer: RepositorySigner = Field(default_factory=RepositorySigner)

    def build(self) -> None:
        self._reset_dist()
        self._write_packages()
        self._publish_redirect_assets()
        self._write_default_download_stats()
        self._write_public_key()
        self._write_health_reports_and_site_data()
        self._write_pages_assets()
        self._write_index_page()
        release_path = self._write_release()
        self.signer.sign_release(self.config, release_path=release_path)
        logger.info("built deployable tree at {}", self.paths.dist_dir)

    def _reset_dist(self) -> None:
        if self.paths.dist_dir.exists():
            shutil.rmtree(self.paths.dist_dir)
        self.paths.dist_dir.mkdir(parents=True)

    def _write_packages(self) -> None:
        for arch, artifacts in self.state.artifacts_by_architecture().items():
            package_stanzas = [
                deb.format_control(artifact.apt_package_stanza())
                for artifact in artifacts
            ]
            packages_dir = self.paths.dist_dir / "dists" / self.config.suite / self.state.component / f"binary-{arch}"
            packages_dir.mkdir(parents=True, exist_ok=True)
            packages_text = "\n".join(package_stanzas)
            packages_dir.joinpath("Packages").write_text(packages_text, encoding="utf-8")
            with gzip.open(packages_dir / "Packages.gz", "wb", compresslevel=9) as handle:
                handle.write(packages_text.encode("utf-8"))

    def _publish_redirect_assets(self) -> None:
        redirect.RedirectRulesPublisher(
            state=self.state,
            dist_dir=self.paths.dist_dir,
            json_files=self.json_files,
            redirect_rules_dirname=REDIRECT_RULES_DIRNAME,
            redirect_snapshot_filename=REDIRECT_SNAPSHOT_FILENAME,
        ).write()

    def _write_default_download_stats(self) -> None:
        DownloadStats.empty("not_generated", days=30, clock=self.clock).write(
            self.paths.dist_dir / DOWNLOAD_STATS_FILENAME,
            self.json_files,
        )

    def _write_public_key(self) -> None:
        self.paths.dist_dir.joinpath("key.asc").write_text(self.signer.public_key(self.config), encoding="utf-8")

    def _write_health_reports_and_site_data(self) -> None:
        reports = site_data_module.HealthReports.load_or_not_generated(
            self.state,
            track_health_path=self.paths.track_health_path,
            artifact_health_path=self.paths.artifact_health_path,
            json_files=self.json_files,
            clock=self.clock,
        )
        reports.write_deploy_files(self.paths.dist_dir, self.json_files)
        downloads = DownloadStats.load_or_empty(
            self.paths.dist_dir / DOWNLOAD_STATS_FILENAME,
            json_files=self.json_files,
            days=30,
            clock=self.clock,
        )
        site_data_module.PublishedSiteData(
            state=self.state,
            reports=reports,
            downloads=downloads,
        ).write(self.paths.dist_dir / SITE_DATA_FILENAME, self.json_files)

    def _write_pages_assets(self) -> None:
        publish.write_headers(
            self.paths.dist_dir / "_headers",
            site_data_filename=SITE_DATA_FILENAME,
            site_data_browser_ttl_policy=SITE_DATA_BROWSER_TTL_POLICY,
            site_data_cdn_ttl_policy=SITE_DATA_CDN_TTL_POLICY,
            redirect_rules_dirname=REDIRECT_RULES_DIRNAME,
            static_redirect_rules_browser_ttl_policy=STATIC_REDIRECT_RULES_BROWSER_TTL_POLICY,
            static_redirect_rules_cdn_ttl_policy=STATIC_REDIRECT_RULES_CDN_TTL_POLICY,
        )
        publish.write_worker(self.paths.dist_dir / "_worker.js", self.paths.worker_script_path)
        publish.write_routes(self.paths.dist_dir / "_routes.json")

    def _write_index_page(self) -> None:
        suite = html_module.escape(self.config.suite)
        component = html_module.escape(self.config.component)
        package_index_links = "\n                ".join(
            f'<a href="/dists/{suite}/{component}/binary-{html_module.escape(arch)}/Packages">Packages {html_module.escape(arch)}</a>'
            for arch in self.state.architectures
        )
        template = self.paths.static_dir.joinpath("index.html").read_text(encoding="utf-8")
        html = (
            template
            .replace("__BASE_URL__", self.config.repository.base_url)
            .replace("__SUITE__", self.config.suite)
            .replace("__COMPONENT__", self.config.component)
            .replace("__PACKAGE_INDEX_LINKS__", package_index_links)
        )
        self.paths.dist_dir.joinpath("index.html").write_text(html, encoding="utf-8")
        for filename in STATIC_ASSET_FILENAMES:
            shutil.copy2(self.paths.static_dir / filename, self.paths.dist_dir / filename)

    def _write_release(self) -> Path:
        suite = self.config.suite
        component = self.config.component
        release_path = self.paths.dist_dir / "dists" / suite / "Release"
        files = []
        for path in sorted((self.paths.dist_dir / "dists" / suite).rglob("*")):
            if path.is_file() and path.name != "Release":
                rel = path.relative_to(self.paths.dist_dir / "dists" / suite).as_posix()
                data = path.read_bytes()
                files.append(
                    (
                        rel,
                        len(data),
                        hashlib.md5(data).hexdigest(),
                        hashlib.sha1(data).hexdigest(),
                        hashlib.sha256(data).hexdigest(),
                    )
                )

        sections = {
            "MD5Sum": [(md5, size, rel) for rel, size, md5, _, _ in files],
            "SHA1": [(sha1, size, rel) for rel, size, _, sha1, _ in files],
            "SHA256": [(sha256, size, rel) for rel, size, _, _, sha256 in files],
        }
        lines = [
            f"Origin: {self.config.repository.origin}",
            f"Label: {self.config.repository.label}",
            f"Suite: {suite}",
            f"Codename: {suite}",
            f"Date: {email.utils.format_datetime(self.clock.utc_now().astimezone(timezone.utc), usegmt=True)}",
            f"Architectures: {' '.join(self.state.architectures)}",
            f"Components: {component}",
            f"Description: {self.config.repository.description}",
        ]
        for name, values in sections.items():
            lines.append(f"{name}:")
            lines.extend(f" {digest} {size:16d} {rel}" for digest, size, rel in values)
        release_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return release_path
