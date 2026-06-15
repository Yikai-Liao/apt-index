from __future__ import annotations

from pathlib import Path


def resolve_root() -> Path:
    cwd = Path.cwd()
    if (cwd / "apt-index.toml").exists():
        return cwd

    source_root = Path(__file__).resolve().parents[1]
    if (source_root / "apt-index.toml").exists():
        return source_root
    return Path(__file__).resolve().parents[2]


ROOT = resolve_root()

LOCK_PATH = ROOT / "apt-index.lock.json"
TRACK_HEALTH_PATH = ROOT / "track_health.json"
ARTIFACT_HEALTH_PATH = ROOT / "artifact_health.json"
STATIC_DIR = ROOT / "static"
CACHE_DIR = ROOT / ".apt-index-cache"
DIST_DIR = ROOT / "dist"
GNUPG_DIR = ROOT / ".apt-index-gnupg"
ENV_PATH = ROOT / ".env"

WORKER_SCRIPT_PATH = Path(__file__).with_name("worker.js")
