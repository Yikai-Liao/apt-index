#!/usr/bin/env python3
from __future__ import annotations

import email.utils
import fnmatch
import gzip
import hashlib
import json
import lzma
import os
import shutil
import subprocess
import tarfile
import tempfile
import tomllib
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import typer
from loguru import logger

ROOT = Path(__file__).resolve().parents[1]
if not (ROOT / "packages.toml").exists():
    ROOT = Path(__file__).resolve().parents[2]
CONFIG_PATH = ROOT / "packages.toml"
LOCK_PATH = ROOT / "apt-index.lock.json"
TRACK_HEALTH_PATH = ROOT / "track_health.json"
ARTIFACT_HEALTH_PATH = ROOT / "artifact_health.json"
CACHE_DIR = ROOT / ".apt-index-cache"
DIST_DIR = ROOT / "dist"
GNUPG_DIR = ROOT / ".apt-index-gnupg"
USER_AGENT = "apt-index/0.1"
DEFAULT_JOBS = 4
app = typer.Typer(no_args_is_help=True)


@dataclass(frozen=True)
class ArtifactCandidate:
    url: str
    upstream_version: str
    asset_name: str
    expected_sha256: str | None = None


@app.command("refresh")
def refresh_command(
    jobs: int | None = typer.Option(None, "--jobs", "-j", help="Maximum package refresh workers."),
) -> None:
    """Resolve upstream artifacts and write generated state."""
    refresh(jobs)


@app.command("build")
def build_command() -> None:
    """Build the deployable APT tree from the lockfile."""
    build()


@app.command("all")
def all_command(
    jobs: int | None = typer.Option(None, "--jobs", "-j", help="Maximum package refresh workers."),
) -> None:
    """Refresh state and build the deployable APT tree."""
    refresh(jobs)
    build()


def refresh(jobs: int | None = None) -> None:
    config = load_config()
    lock = load_json(LOCK_PATH, {"version": 1, "generated_at": None, "packages": {}})
    previous_packages = lock.get("packages", {})
    locked_packages: dict[str, Any] = {}
    track_health: dict[str, Any] = {"version": 1, "generated_at": now_iso(), "packages": {}}
    package_entries = list(config["packages"].items())
    max_workers = worker_count(len(package_entries), jobs)

    logger.info("refreshing {} package entries with {} workers", len(package_entries), max_workers)
    resolved_entries: dict[str, Any] = {}
    errors: dict[str, Exception] = {}
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {
            executor.submit(resolve_entry, config, entry_name, entry): entry_name
            for entry_name, entry in package_entries
        }
        for future in as_completed(futures):
            entry_name = futures[future]
            try:
                resolved_entries[entry_name] = future.result()
            except Exception as exc:
                errors[entry_name] = exc

    for entry_name, _ in package_entries:
        if entry_name in resolved_entries:
            resolved = resolved_entries[entry_name]
            locked_packages[entry_name] = resolved
            track_health["packages"][entry_name] = {"status": "ok", "artifacts": list(resolved["artifacts"].keys())}
            continue
        if entry_name in errors:
            exc = errors[entry_name]
            if entry_name in previous_packages:
                locked_packages[entry_name] = previous_packages[entry_name]
                status = "kept_previous"
            else:
                status = "failed"
            track_health["packages"][entry_name] = {"status": status, "error": str(exc)}
            logger.warning("{} refresh {}: {}", entry_name, status, exc)

    lock = {"version": 1, "generated_at": now_iso(), "packages": locked_packages}
    write_json(LOCK_PATH, lock)
    artifact_health = check_artifacts(lock, max_workers)
    write_json(TRACK_HEALTH_PATH, track_health)
    write_json(ARTIFACT_HEALTH_PATH, artifact_health)

    failed_required = [
        name
        for name, health in track_health["packages"].items()
        if health["status"] == "failed"
    ]
    if failed_required:
        raise SystemExit(f"failed to resolve required package entries: {', '.join(failed_required)}")


def build() -> None:
    config = load_config()
    lock = load_json(LOCK_PATH, None)
    if not lock:
        raise SystemExit("apt-index.lock.json is missing; run refresh first")

    if DIST_DIR.exists():
        shutil.rmtree(DIST_DIR)
    DIST_DIR.mkdir(parents=True)

    suite = config["suite"]
    component = config["component"]
    archs = config["required_architectures"] + config.get("optional_architectures", [])
    redirect_rules: dict[str, str] = {}

    for arch in archs:
        package_stanzas: list[str] = []
        for entry_name, entry in lock["packages"].items():
            artifact = entry["artifacts"].get(arch)
            if not artifact:
                continue
            filename = artifact["filename"]
            virtual_path = f"pool/{component}/{entry_name}/{filename}"
            redirect_rules["/" + virtual_path] = artifact["url"]
            stanza = dict(artifact["control"])
            stanza["Filename"] = virtual_path
            stanza["Size"] = str(artifact["size"])
            stanza["MD5sum"] = artifact["md5"]
            stanza["SHA1"] = artifact["sha1"]
            stanza["SHA256"] = artifact["sha256"]
            package_stanzas.append(format_control(stanza))

        packages_dir = DIST_DIR / "dists" / suite / component / f"binary-{arch}"
        packages_dir.mkdir(parents=True, exist_ok=True)
        packages_text = "\n".join(package_stanzas)
        (packages_dir / "Packages").write_text(packages_text, encoding="utf-8")
        with gzip.open(packages_dir / "Packages.gz", "wb", compresslevel=9) as f:
            f.write(packages_text.encode("utf-8"))

    (DIST_DIR / "redirect_rules.json").write_text(json.dumps(redirect_rules, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    (DIST_DIR / "key.asc").write_text(ensure_signing_key(config), encoding="utf-8")
    write_worker(DIST_DIR / "_worker.js")
    write_routes(DIST_DIR / "_routes.json")
    write_index_page(config, lock)
    write_release(config, archs)
    sign_release(config)
    logger.info("built deployable tree at {}", DIST_DIR)


def resolve_entry(config: dict[str, Any], entry_name: str, entry: dict[str, Any]) -> dict[str, Any]:
    logger.info("resolving {}", entry_name)
    artifacts: dict[str, Any] = {}
    for arch in config["required_architectures"] + config.get("optional_architectures", []):
        candidate = resolve_candidate(entry, arch)
        deb_path = download(candidate.url, candidate.expected_sha256)
        metadata = inspect_deb(deb_path)
        control = metadata["control"]
        package_arch = control.get("Architecture")
        if package_arch not in {arch, "all"}:
            raise RuntimeError(f"{entry_name}:{arch} resolved package architecture {package_arch!r}")
        artifacts[arch] = {
            "url": candidate.url,
            "upstream_version": candidate.upstream_version,
            "asset_name": candidate.asset_name,
            "filename": safe_deb_filename(control, candidate.asset_name),
            "control": control,
            "size": metadata["size"],
            "md5": metadata["md5"],
            "sha1": metadata["sha1"],
            "sha256": metadata["sha256"],
        }

    return {
        "update_policy": entry["update_policy"],
        "source": entry["source"],
        "resolved_at": now_iso(),
        "artifacts": artifacts,
    }


def resolve_candidate(entry: dict[str, Any], arch: str) -> ArtifactCandidate:
    source = entry["source"]
    if source == "github":
        release = github_release(entry)
        pattern = entry.get("asset_patterns", {}).get(arch)
        if not pattern:
            raise RuntimeError(f"missing GitHub asset pattern for {arch}")
        for asset in release.get("assets", []):
            name = asset["name"]
            if fnmatch.fnmatch(name, pattern):
                return ArtifactCandidate(asset["browser_download_url"], release["tag_name"], name)
        raise RuntimeError(f"no GitHub asset matched {pattern!r}")
    if source == "aur":
        srcinfo = fetch_text(f"https://aur.archlinux.org/cgit/aur.git/plain/.SRCINFO?h={entry['aur_package']}")
        aur_arch = entry.get("aur_architectures", {}).get(arch)
        if not aur_arch:
            raise RuntimeError(f"missing AUR architecture mapping for {arch}")
        fields = parse_srcinfo(srcinfo)
        url = fields.get(f"source_{aur_arch}")
        sha256 = fields.get(f"sha256sums_{aur_arch}")
        if not url:
            raise RuntimeError(f"AUR source_{aur_arch} is missing")
        asset_name, artifact_url = split_aur_source(url)
        return ArtifactCandidate(artifact_url, fields.get("pkgver", "unknown"), asset_name, sha256)
    raise RuntimeError(f"unsupported source resolver {source!r}")


def github_release(entry: dict[str, Any]) -> dict[str, Any]:
    repo = entry["repo"]
    if entry["update_policy"] == "fixed":
        path = f"repos/{repo}/releases/tags/{entry['release_tag']}"
    else:
        path = f"repos/{repo}/releases/latest"

    headers = {}
    token = os.environ.get("GITHUB_TOKEN")
    if token:
        headers["Authorization"] = f"Bearer {token}"
        return fetch_json(f"https://api.github.com/{path}", headers=headers)

    gh = shutil.which("gh")
    if gh:
        result = subprocess.run([gh, "api", path], cwd=ROOT, check=True, text=True, capture_output=True)
        return json.loads(result.stdout)

    return fetch_json(f"https://api.github.com/{path}", headers=headers)


def parse_srcinfo(text: str) -> dict[str, str]:
    fields: dict[str, str] = {}
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or "=" not in line:
            continue
        key, value = line.split("=", 1)
        fields.setdefault(key.strip(), value.strip())
    return fields


def split_aur_source(value: str) -> tuple[str, str]:
    if "::" in value:
        asset_name, url = value.split("::", 1)
        return asset_name, url
    return Path(value).name, value


def download(url: str, expected_sha256: str | None = None) -> Path:
    CACHE_DIR.mkdir(exist_ok=True)
    cache_key = hashlib.sha256(url.encode("utf-8")).hexdigest()
    path = CACHE_DIR / f"{cache_key}.deb"
    if not path.exists():
        logger.info("downloading {}", url)
        request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        tmp_path: Path | None = None
        try:
            with urllib.request.urlopen(request, timeout=120) as response:
                with tempfile.NamedTemporaryFile(dir=CACHE_DIR, delete=False) as tmp:
                    tmp_path = Path(tmp.name)
                    shutil.copyfileobj(response, tmp, length=1024 * 1024)
            if tmp_path:
                tmp_path.replace(path)
        except Exception:
            if tmp_path:
                tmp_path.unlink(missing_ok=True)
            raise
    if expected_sha256 and file_hash(path, "sha256") != expected_sha256:
        path.unlink(missing_ok=True)
        raise RuntimeError(f"sha256 mismatch for {url}")
    return path


def inspect_deb(path: Path) -> dict[str, Any]:
    members = read_ar(path)
    control_name = next((name for name in members if name.startswith("control.tar")), None)
    if not control_name:
        raise RuntimeError(f"{path.name} has no control.tar member")
    control_bytes = extract_control_tar(members[control_name], control_name)
    control = read_control_file(control_bytes, control_name)
    data = path.read_bytes()
    return {
        "control": control,
        "size": len(data),
        "md5": hashlib.md5(data).hexdigest(),
        "sha1": hashlib.sha1(data).hexdigest(),
        "sha256": hashlib.sha256(data).hexdigest(),
    }


def read_ar(path: Path) -> dict[str, bytes]:
    data = path.read_bytes()
    if not data.startswith(b"!<arch>\n"):
        raise RuntimeError(f"{path.name} is not a Debian ar archive")
    offset = 8
    members: dict[str, bytes] = {}
    while offset + 60 <= len(data):
        header = data[offset : offset + 60]
        name = header[:16].decode("utf-8").strip().rstrip("/")
        size = int(header[48:58].decode("utf-8").strip())
        start = offset + 60
        end = start + size
        members[name] = data[start:end]
        offset = end + (size % 2)
    return members


def extract_control_tar(data: bytes, name: str) -> bytes:
    if name.endswith(".gz"):
        return gzip.decompress(data)
    if name.endswith(".xz"):
        return lzma.decompress(data)
    if name.endswith(".zst"):
        zstd = shutil.which("zstd")
        if not zstd:
            raise RuntimeError("zstd is required to read control.tar.zst")
        result = subprocess.run([zstd, "-d", "-q", "-c"], input=data, check=True, capture_output=True)
        return result.stdout
    if name.endswith(".tar"):
        return data
    raise RuntimeError(f"unsupported control archive format {name}")


def read_control_file(tar_bytes: bytes, member_name: str) -> dict[str, str]:
    with tempfile.TemporaryDirectory() as tmp:
        tar_path = Path(tmp) / member_name
        tar_path.write_bytes(tar_bytes)
        with tarfile.open(tar_path) as archive:
            control_member = next((m for m in archive.getmembers() if m.name in {"control", "./control"}), None)
            if not control_member:
                raise RuntimeError("control file is missing")
            extracted = archive.extractfile(control_member)
            if not extracted:
                raise RuntimeError("control file could not be read")
            return parse_control(extracted.read().decode("utf-8", errors="replace"))


def parse_control(text: str) -> dict[str, str]:
    fields: dict[str, str] = {}
    current_key: str | None = None
    for line in text.splitlines():
        if not line:
            continue
        if line[0].isspace() and current_key:
            fields[current_key] += "\n" + line
            continue
        key, value = line.split(":", 1)
        current_key = key
        fields[key] = value.strip()
    return fields


def format_control(fields: dict[str, str]) -> str:
    preferred = [
        "Package",
        "Version",
        "Architecture",
        "Maintainer",
        "Installed-Size",
        "Depends",
        "Recommends",
        "Suggests",
        "Section",
        "Priority",
        "Homepage",
        "Description",
        "Filename",
        "Size",
        "MD5sum",
        "SHA1",
        "SHA256",
    ]
    keys = preferred + sorted(k for k in fields if k not in preferred)
    return "\n".join(f"{key}: {fields[key]}" for key in keys if fields.get(key)) + "\n"


def safe_deb_filename(control: dict[str, str], asset_name: str) -> str:
    package = control["Package"].replace("/", "_")
    version = control["Version"].replace(":", "%3a").replace("/", "_")
    arch = control["Architecture"]
    if asset_name.endswith(".deb") and all(part in asset_name for part in [arch]):
        return asset_name
    return f"{package}_{version}_{arch}.deb"


def check_artifacts(lock: dict[str, Any], jobs: int) -> dict[str, Any]:
    health = {"version": 1, "generated_at": now_iso(), "packages": {}}
    artifact_entries = [
        (entry_name, arch, artifact)
        for entry_name, entry in lock["packages"].items()
        for arch, artifact in entry["artifacts"].items()
    ]
    max_workers = worker_count(len(artifact_entries), jobs)
    checked: dict[tuple[str, str], dict[str, Any]] = {}
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {
            executor.submit(check_artifact, artifact): (entry_name, arch)
            for entry_name, arch, artifact in artifact_entries
        }
        for future in as_completed(futures):
            key = futures[future]
            try:
                checked[key] = future.result()
            except Exception as exc:
                checked[key] = {"status": "failed", "error": str(exc)}
    for entry_name, entry in lock["packages"].items():
        health["packages"][entry_name] = {
            "artifacts": {
                arch: checked[(entry_name, arch)]
                for arch in entry["artifacts"]
            }
        }
    return health


def check_artifact(artifact: dict[str, Any]) -> dict[str, Any]:
    path = download(artifact["url"], artifact["sha256"])
    return {
        "status": "ok",
        "size": path.stat().st_size,
        "sha256": file_hash(path, "sha256"),
    }


def write_release(config: dict[str, Any], archs: list[str]) -> None:
    suite = config["suite"]
    component = config["component"]
    release_path = DIST_DIR / "dists" / suite / "Release"
    files = []
    for path in sorted((DIST_DIR / "dists" / suite).rglob("*")):
        if path.is_file() and path.name != "Release":
            rel = path.relative_to(DIST_DIR / "dists" / suite).as_posix()
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


def ensure_signing_key(config: dict[str, Any]) -> str:
    GNUPG_DIR.mkdir(mode=0o700, exist_ok=True)
    key_name = config["signing"]["key_name"]
    env = gpg_env()
    list_result = subprocess.run(["gpg", "--list-secret-keys", key_name], env=env, text=True, capture_output=True)
    if list_result.returncode != 0:
        subprocess.run(
            [
                "gpg",
                "--batch",
                "--pinentry-mode",
                "loopback",
                "--passphrase",
                "",
                "--quick-generate-key",
                key_name,
                "rsa3072",
                "sign",
                "0",
            ],
            env=env,
            check=True,
        )
    result = subprocess.run(["gpg", "--armor", "--export", key_name], env=env, check=True, text=True, capture_output=True)
    return result.stdout


def sign_release(config: dict[str, Any]) -> None:
    ensure_signing_key(config)
    key_name = config["signing"]["key_name"]
    release_path = DIST_DIR / "dists" / config["suite"] / "Release"
    inrelease_path = release_path.parent / "InRelease"
    release_gpg_path = release_path.parent / "Release.gpg"
    env = gpg_env()
    subprocess.run(
        ["gpg", "--batch", "--yes", "--pinentry-mode", "loopback", "--passphrase", "", "--local-user", key_name, "--clearsign", "--digest-algo", "SHA256", "--output", str(inrelease_path), str(release_path)],
        env=env,
        check=True,
    )
    subprocess.run(
        ["gpg", "--batch", "--yes", "--pinentry-mode", "loopback", "--passphrase", "", "--local-user", key_name, "--detach-sign", "--armor", "--digest-algo", "SHA256", "--output", str(release_gpg_path), str(release_path)],
        env=env,
        check=True,
    )


def write_worker(path: Path) -> None:
    path.write_text(
        """export default {
  async fetch(request, env) {
    const url = new URL(request.url);
    const rulesUrl = new URL("/redirect_rules.json", url);
    const rulesResponse = await env.ASSETS.fetch(rulesUrl.toString());
    if (!rulesResponse.ok) {
      return new Response("redirect rules unavailable", { status: 503 });
    }
    const rules = await rulesResponse.json();
    const target = rules[url.pathname];
    if (!target) {
      return new Response("package redirect not found", { status: 404 });
    }
    return Response.redirect(target, 302);
  },
};
""",
        encoding="utf-8",
    )


def write_routes(path: Path) -> None:
    routes = {"version": 1, "include": ["/pool/*"], "exclude": []}
    path.write_text(json.dumps(routes, indent=2) + "\n", encoding="utf-8")


def write_index_page(config: dict[str, Any], lock: dict[str, Any]) -> None:
    base_url = config["repository"]["base_url"]
    packages = []
    for entry in lock["packages"].values():
        for artifact in entry["artifacts"].values():
            control = artifact["control"]
            packages.append(f"<li><code>{control['Package']}</code> {control['Version']} ({control['Architecture']})</li>")
    html = f"""<!doctype html>
<meta charset="utf-8">
<title>Apt Index</title>
<h1>Apt Index</h1>
<p>APT source:</p>
<pre>curl -fsSL {base_url}/key.asc | sudo gpg --dearmor -o /usr/share/keyrings/lyk-ai-apt.gpg
echo "deb [signed-by=/usr/share/keyrings/lyk-ai-apt.gpg] {base_url} {config['suite']} {config['component']}" | sudo tee /etc/apt/sources.list.d/lyk-ai.list
sudo apt update</pre>
<ul>
{''.join(packages)}
</ul>
"""
    (DIST_DIR / "index.html").write_text(html, encoding="utf-8")


def load_config() -> dict[str, Any]:
    with CONFIG_PATH.open("rb") as f:
        return tomllib.load(f)


def load_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, data: Any) -> None:
    path.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def fetch_json(url: str, headers: dict[str, str] | None = None) -> Any:
    return json.loads(fetch_text(url, headers))


def fetch_text(url: str, headers: dict[str, str] | None = None) -> str:
    request_headers = {"User-Agent": USER_AGENT}
    request_headers.update(headers or {})
    request = urllib.request.Request(url, headers=request_headers)
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            return response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"GET {url} failed: HTTP {exc.code}: {body}") from exc


def file_hash(path: Path, algorithm: str) -> str:
    h = hashlib.new(algorithm)
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def gpg_env() -> dict[str, str]:
    env = os.environ.copy()
    env["GNUPGHOME"] = str(GNUPG_DIR)
    return env


def worker_count(total: int, requested: int | None) -> int:
    if requested is None:
        env_value = os.environ.get("APT_INDEX_JOBS")
        requested = int(env_value) if env_value else DEFAULT_JOBS
    if requested < 1:
        raise typer.BadParameter("jobs must be at least 1")
    return max(1, min(total or 1, requested))


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


if __name__ == "__main__":
    app()
