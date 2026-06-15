from __future__ import annotations

import gzip
import hashlib
import lzma
import shutil
import subprocess
import tarfile
import tempfile
import urllib.request
from pathlib import Path
from typing import Any

from loguru import logger


def download(
    url: str,
    *,
    cache_dir: Path,
    user_agent: str,
    expected_hash: str | None = None,
    hash_algorithm: str = "sha256",
) -> Path:
    cache_dir.mkdir(exist_ok=True)
    cache_key = hashlib.sha256(url.encode("utf-8")).hexdigest()
    path = cache_dir / f"{cache_key}.deb"
    if not path.exists():
        logger.info("downloading {}", url)
        request = urllib.request.Request(url, headers={"User-Agent": user_agent})
        tmp_path: Path | None = None
        try:
            with urllib.request.urlopen(request, timeout=120) as response:
                with tempfile.NamedTemporaryFile(dir=cache_dir, delete=False) as tmp:
                    tmp_path = Path(tmp.name)
                    shutil.copyfileobj(response, tmp, length=1024 * 1024)
            if tmp_path:
                tmp_path.replace(path)
        except Exception:
            if tmp_path:
                tmp_path.unlink(missing_ok=True)
            raise
    if expected_hash and file_hash(path, hash_algorithm) != expected_hash:
        path.unlink(missing_ok=True)
        raise RuntimeError(f"downloaded artifact hash mismatch for {url}")
    return path


def inspect_deb(path: Path) -> dict[str, Any]:
    members = read_ar(path)
    control_name = next((name for name in members if name.startswith("control.tar")), None)
    if not control_name:
        raise RuntimeError("deb is missing control.tar member")
    control_bytes = extract_control_tar(members[control_name])
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
        raise RuntimeError("deb does not start with ar magic")
    offset = 8
    members: dict[str, bytes] = {}
    while offset + 60 <= len(data):
        header = data[offset : offset + 60]
        offset += 60
        name = header[:16].decode("utf-8", errors="replace").strip().rstrip("/")
        size = int(header[48:58].decode("ascii").strip())
        members[name] = data[offset : offset + size]
        offset += size + (size % 2)
    return members


def extract_control_tar(data: bytes) -> bytes:
    if data.startswith(b"\x1f\x8b"):
        return gzip.decompress(data)
    if data.startswith(b"\xfd7zXZ"):
        return lzma.decompress(data)
    if data.startswith(b"(\xb5/\xfd"):
        zstd = shutil.which("zstd")
        if not zstd:
            raise RuntimeError("zstd is required to read control.tar.zst")
        result = subprocess.run([zstd, "-d", "-q", "-c"], input=data, check=True, capture_output=True)
        return result.stdout
    if data.startswith(b"./") or data.startswith(b"control"):
        return data
    raise RuntimeError("unsupported control.tar compression")


def read_control_file(tar_bytes: bytes, member_name: str) -> dict[str, str]:
    with tempfile.NamedTemporaryFile(suffix=member_name) as tmp:
        tmp.write(tar_bytes)
        tmp.flush()
        with tarfile.open(tmp.name) as tar:
            for member in tar.getmembers():
                if Path(member.name).name != "control":
                    continue
                extracted = tar.extractfile(member)
                if not extracted:
                    raise RuntimeError("control file could not be read")
                return parse_control(extracted.read().decode("utf-8", errors="replace"))
    raise RuntimeError("control archive is missing control file")


def parse_control(text: str) -> dict[str, str]:
    fields: dict[str, str] = {}
    current_key: str | None = None
    for line in text.splitlines():
        if not line:
            current_key = None
            continue
        if line[0].isspace() and current_key:
            fields[current_key] += "\n" + line
            continue
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        current_key = key
        fields[key] = value.strip()
    return fields


def format_control(fields: dict[str, str]) -> str:
    preferred_order = [
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
        "Filename",
        "Size",
        "MD5sum",
        "SHA1",
        "SHA256",
        "Description",
    ]
    keys = preferred_order + sorted(key for key in fields if key not in preferred_order)
    return "\n".join(f"{key}: {fields[key]}" for key in keys if fields.get(key)) + "\n"


def safe_deb_filename(control: dict[str, str], asset_name: str) -> str:
    package = control["Package"].replace("/", "_")
    version = control["Version"].replace(":", "%3a").replace("/", "_")
    arch = control["Architecture"]
    if asset_name.endswith(".deb") and package in asset_name and arch in asset_name:
        return asset_name
    return f"{package}_{version}_{arch}.deb"


def file_hash(path: Path, algorithm: str) -> str:
    h = hashlib.new(algorithm)
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()
