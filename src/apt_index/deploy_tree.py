from __future__ import annotations

import base64
import email.utils
import gzip
import hashlib
import html as html_module
import json
import os
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from loguru import logger

from apt_index import deb, download_stats as download_stats_module, publish, redirect, site_data as site_data_module
from apt_index.paths import ARTIFACT_HEALTH_PATH, DIST_DIR, ENV_PATH, GNUPG_DIR, STATIC_DIR, TRACK_HEALTH_PATH, WORKER_SCRIPT_PATH
from apt_index.published_state import PublishedState

JsonLoader = Callable[[Path, Any], Any]
JsonWriter = Callable[[Path, Any], None]
TimestampFactory = Callable[[], str]

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
DOTENV_LOADED = False


def build_deployable_tree(
    config: dict[str, Any],
    state: PublishedState,
    *,
    dist_dir: Path = DIST_DIR,
) -> None:
    if dist_dir.exists():
        shutil.rmtree(dist_dir)
    dist_dir.mkdir(parents=True)

    for arch in state.architectures():
        package_stanzas = []
        for artifact in state.artifacts_for_arch(arch):
            package_stanzas.append(deb.format_control(artifact.package_stanza()))

        packages_dir = dist_dir / "dists" / config["suite"] / state.component / f"binary-{arch}"
        packages_dir.mkdir(parents=True, exist_ok=True)
        packages_text = "\n".join(package_stanzas)
        (packages_dir / "Packages").write_text(packages_text, encoding="utf-8")
        with gzip.open(packages_dir / "Packages.gz", "wb", compresslevel=9) as handle:
            handle.write(packages_text.encode("utf-8"))

    redirect.write_redirect_rules(
        state,
        dist_dir=dist_dir,
        redirect_rules_dirname=REDIRECT_RULES_DIRNAME,
        redirect_snapshot_filename=REDIRECT_SNAPSHOT_FILENAME,
        write_json=write_json,
    )
    write_json(dist_dir / DOWNLOAD_STATS_FILENAME, download_stats_module.empty_download_stats("not_generated", 30, now_iso))
    (dist_dir / "key.asc").write_text(ensure_signing_key(config), encoding="utf-8")
    site_data_module.copy_state_files(
        state,
        track_health_path=TRACK_HEALTH_PATH,
        artifact_health_path=ARTIFACT_HEALTH_PATH,
        dist_dir=dist_dir,
        write_json=write_json,
        now_iso=now_iso,
    )
    site_data_module.write_site_data(
        dist_dir / SITE_DATA_FILENAME,
        dist_dir / DOWNLOAD_STATS_FILENAME,
        state=state,
        track_health_path=TRACK_HEALTH_PATH,
        artifact_health_path=ARTIFACT_HEALTH_PATH,
        load_json=load_json,
        write_json=write_json,
        empty_download_stats=lambda reason: download_stats_module.empty_download_stats(reason, 30, now_iso),
        now_iso=now_iso,
    )
    publish.write_headers(
        dist_dir / "_headers",
        site_data_filename=SITE_DATA_FILENAME,
        site_data_browser_ttl_policy=SITE_DATA_BROWSER_TTL_POLICY,
        site_data_cdn_ttl_policy=SITE_DATA_CDN_TTL_POLICY,
        redirect_rules_dirname=REDIRECT_RULES_DIRNAME,
        static_redirect_rules_browser_ttl_policy=STATIC_REDIRECT_RULES_BROWSER_TTL_POLICY,
        static_redirect_rules_cdn_ttl_policy=STATIC_REDIRECT_RULES_CDN_TTL_POLICY,
    )
    publish.write_worker(dist_dir / "_worker.js", WORKER_SCRIPT_PATH)
    publish.write_routes(dist_dir / "_routes.json")
    write_index_page(config, state, dist_dir=dist_dir)
    write_release(config, state.architectures(), dist_dir=dist_dir)
    sign_release(config, dist_dir=dist_dir)
    logger.info("built deployable tree at {}", dist_dir)


def write_release(config: dict[str, Any], archs: list[str], *, dist_dir: Path = DIST_DIR) -> None:
    suite = config["suite"]
    component = config["component"]
    release_path = dist_dir / "dists" / suite / "Release"
    files = []
    for path in sorted((dist_dir / "dists" / suite).rglob("*")):
        if path.is_file() and path.name != "Release":
            rel = path.relative_to(dist_dir / "dists" / suite).as_posix()
            data = path.read_bytes()
            files.append((rel, len(data), hashlib.md5(data).hexdigest(), hashlib.sha1(data).hexdigest(), hashlib.sha256(data).hexdigest()))

    sections = {
        "MD5Sum": [(md5, size, rel) for rel, size, md5, _, _ in files],
        "SHA1": [(sha1, size, rel) for rel, size, _, sha1, _ in files],
        "SHA256": [(sha256, size, rel) for rel, size, _, _, sha256 in files],
    }
    repo = config["repository"]
    lines = [
        f"Origin: {repo['origin']}",
        f"Label: {repo['label']}",
        f"Suite: {suite}",
        f"Codename: {suite}",
        f"Date: {email.utils.format_datetime(datetime.now(timezone.utc), usegmt=True)}",
        f"Architectures: {' '.join(archs)}",
        f"Components: {component}",
        f"Description: {repo['description']}",
    ]
    for name, values in sections.items():
        lines.append(f"{name}:")
        lines.extend(f" {digest} {size:16d} {rel}" for digest, size, rel in values)
    release_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def load_dotenv() -> None:
    global DOTENV_LOADED
    if DOTENV_LOADED:
        return
    DOTENV_LOADED = True
    if not ENV_PATH.exists():
        return

    for raw_line in ENV_PATH.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line.removeprefix("export ").strip()
        name, value = line.split("=", 1)
        name = name.strip()
        value = value.strip()
        if not name or name in os.environ:
            continue
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        value = value.replace("\\n", "\n")
        os.environ[name] = value


def has_secret_key(key_name: str, env: dict[str, str]) -> bool:
    list_result = subprocess.run(["gpg", "--list-secret-keys", key_name], env=env, text=True, capture_output=True)
    return list_result.returncode == 0


def import_signing_key_from_env(env: dict[str, str]) -> None:
    key_material = os.environ.get(SIGNING_PRIVATE_KEY_ENV)
    key_material_b64 = os.environ.get(SIGNING_PRIVATE_KEY_B64_ENV)
    if not key_material and key_material_b64:
        key_material = base64.b64decode(key_material_b64).decode("utf-8")
    if not key_material:
        raise RuntimeError(
            "missing signing private key; set "
            f"{SIGNING_PRIVATE_KEY_B64_ENV} or {SIGNING_PRIVATE_KEY_ENV} before running apt-index build"
        )

    subprocess.run(
        [
            "gpg",
            "--batch",
            "--yes",
            "--pinentry-mode",
            "loopback",
            "--passphrase",
            os.environ.get(SIGNING_PASSPHRASE_ENV, ""),
            "--import",
        ],
        input=key_material,
        env=env,
        text=True,
        check=True,
    )


def ensure_signing_key(config: dict[str, Any]) -> str:
    load_dotenv()
    GNUPG_DIR.mkdir(mode=0o700, exist_ok=True)
    key_name = config["signing"]["key_name"]
    env = gpg_env()
    if not has_secret_key(key_name, env):
        import_signing_key_from_env(env)
    if not has_secret_key(key_name, env):
        raise RuntimeError(f"imported signing key does not contain secret key for {key_name!r}")
    result = subprocess.run(["gpg", "--armor", "--export", key_name], env=env, check=True, text=True, capture_output=True)
    return result.stdout


def sign_release(config: dict[str, Any], *, dist_dir: Path = DIST_DIR) -> None:
    ensure_signing_key(config)
    key_name = config["signing"]["key_name"]
    release_path = dist_dir / "dists" / config["suite"] / "Release"
    inrelease_path = release_path.parent / "InRelease"
    release_gpg_path = release_path.parent / "Release.gpg"
    env = gpg_env()
    passphrase = os.environ.get(SIGNING_PASSPHRASE_ENV, "")
    subprocess.run(
        [
            "gpg",
            "--batch",
            "--yes",
            "--pinentry-mode",
            "loopback",
            "--passphrase",
            passphrase,
            "--local-user",
            key_name,
            "--clearsign",
            "--digest-algo",
            "SHA256",
            "--output",
            str(inrelease_path),
            str(release_path),
        ],
        env=env,
        check=True,
    )
    subprocess.run(
        [
            "gpg",
            "--batch",
            "--yes",
            "--pinentry-mode",
            "loopback",
            "--passphrase",
            passphrase,
            "--local-user",
            key_name,
            "--detach-sign",
            "--armor",
            "--digest-algo",
            "SHA256",
            "--output",
            str(release_gpg_path),
            str(release_path),
        ],
        env=env,
        check=True,
    )


def write_index_page(config: dict[str, Any], state: PublishedState, *, dist_dir: Path = DIST_DIR) -> None:
    base_url = config["repository"]["base_url"]
    suite = html_module.escape(config["suite"])
    component = html_module.escape(config["component"])
    package_index_links = "\n                ".join(
        f'<a href="/dists/{suite}/{component}/binary-{html_module.escape(arch)}/Packages">Packages {html_module.escape(arch)}</a>'
        for arch in state.architectures()
    )
    template = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
    html = (
        template
        .replace("__BASE_URL__", base_url)
        .replace("__SUITE__", config["suite"])
        .replace("__COMPONENT__", config["component"])
        .replace("__PACKAGE_INDEX_LINKS__", package_index_links)
    )
    (dist_dir / "index.html").write_text(html, encoding="utf-8")
    for filename in STATIC_ASSET_FILENAMES:
        shutil.copy2(STATIC_DIR / filename, dist_dir / filename)


def load_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, data: Any) -> None:
    path.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def gpg_env() -> dict[str, str]:
    env = os.environ.copy()
    env["GNUPGHOME"] = str(GNUPG_DIR)
    return env


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()
