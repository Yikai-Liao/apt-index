from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any, Callable


JsonLoader = Callable[[Path, Any], Any]
JsonWriter = Callable[[Path, Any], None]
TimestampFactory = Callable[[], str]


def copy_state_files(
    lock: dict[str, Any],
    *,
    track_health_path: Path,
    artifact_health_path: Path,
    dist_dir: Path,
    write_json: JsonWriter,
    now_iso: TimestampFactory,
) -> None:
    copy_or_write_health_report(
        track_health_path,
        dist_dir / track_health_path.name,
        lambda: not_generated_track_health(lock, now_iso),
        write_json,
    )
    copy_or_write_health_report(
        artifact_health_path,
        dist_dir / artifact_health_path.name,
        lambda: not_generated_artifact_health(lock, now_iso),
        write_json,
    )


def copy_or_write_health_report(source: Path, target: Path, fallback_factory: Callable[[], dict[str, Any]], write_json: JsonWriter) -> None:
    if source.exists():
        shutil.copy2(source, target)
        return
    write_json(target, fallback_factory())


def write_site_data(
    output: Path,
    download_stats_path: Path,
    *,
    lock_path: Path,
    track_health_path: Path,
    artifact_health_path: Path,
    load_json: JsonLoader,
    write_json: JsonWriter,
    empty_download_stats: Callable[[str], dict[str, Any]],
    now_iso: TimestampFactory,
) -> None:
    lock = load_json(lock_path, None)
    if not lock:
        raise SystemExit("apt-index.lock.json is missing; run refresh first")
    track_health = load_json(track_health_path, None) or not_generated_track_health(lock, now_iso)
    artifact_health = load_json(artifact_health_path, None) or not_generated_artifact_health(lock, now_iso)
    download_stats = load_json(download_stats_path, None) or empty_download_stats("not_generated")
    output.parent.mkdir(parents=True, exist_ok=True)
    write_json(output, format_site_data(lock, track_health, artifact_health, download_stats))


def not_generated_track_health(lock: dict[str, Any], now_iso: TimestampFactory) -> dict[str, Any]:
    return {
        "version": 2,
        "generated_at": now_iso(),
        "status": "not_generated",
        "packages": {
            entry_name: {
                "status": "not_checked",
                "architectures": {
                    arch: {"status": "not_checked"}
                    for arch, architecture in entry.get("architectures", {}).items()
                    if architecture.get("artifact")
                },
            }
            for entry_name, entry in lock.get("packages", {}).items()
        },
    }


def not_generated_artifact_health(lock: dict[str, Any], now_iso: TimestampFactory) -> dict[str, Any]:
    return {
        "version": 2,
        "generated_at": now_iso(),
        "status": "not_generated",
        "packages": {
            entry_name: {
                "artifacts": {
                    arch: not_checked_artifact_health(architecture["artifact"])
                    for arch, architecture in entry.get("architectures", {}).items()
                    if architecture.get("artifact")
                }
            }
            for entry_name, entry in lock.get("packages", {}).items()
        },
    }


def not_checked_artifact_health(artifact: dict[str, Any]) -> dict[str, Any]:
    health: dict[str, Any] = {"status": "not_checked", "check": "not_generated"}
    for key in ("size", "sha256"):
        if key in artifact:
            health[key] = artifact[key]
    return health


def format_site_data(
    lock: dict[str, Any],
    track_health: dict[str, Any],
    artifact_health: dict[str, Any],
    download_stats: dict[str, Any],
) -> dict[str, Any]:
    downloads_by_identity = {
        (str(row.get("entry_name") or ""), str(row.get("arch") or "")): {
            "downloads": int(row.get("downloads") or 0),
            "last_7_days": int(row.get("last_7_days") or 0),
        }
        for row in download_stats.get("packages", [])
    }
    packages: list[dict[str, Any]] = []
    artifact_count = 0
    total_size = 0
    downloads_last_days = 0
    downloads_last_7_days = 0

    for entry_name, entry in sorted(lock.get("packages", {}).items()):
        grouped_rows: dict[str, dict[str, Any]] = {}
        for arch, architecture in sorted(entry.get("architectures", {}).items()):
            artifact = architecture.get("artifact")
            if not artifact:
                continue
            control = artifact.get("control", {})
            package_name = str(control.get("Package") or entry_name)
            row = grouped_rows.setdefault(
                package_name,
                {
                    "entry_name": entry_name,
                    "package_name": package_name,
                    "description": first_line(control.get("Description")),
                    "homepage": preferred_homepage(entry, control, artifact),
                    "artifacts": [],
                },
            )
            download_row = downloads_by_identity.get((entry_name, arch), {})
            track_status = str(track_health.get("packages", {}).get(entry_name, {}).get("architectures", {}).get(arch, {}).get("status") or "unknown")
            artifact_status = str(artifact_health.get("packages", {}).get(entry_name, {}).get("artifacts", {}).get(arch, {}).get("status") or "unknown")
            row["artifacts"].append(
                {
                    "arch": arch,
                    "version": str(control.get("Version") or ""),
                    "source": str(architecture.get("source") or ""),
                    "update_policy": str(architecture.get("update_policy") or ""),
                    "size": int(artifact.get("size") or 0),
                    "downloads": int(download_row.get("downloads") or 0),
                    "downloads_last_7_days": int(download_row.get("last_7_days") or 0),
                    "track_status": track_status,
                    "artifact_status": artifact_status,
                    "status_class": status_class_for(track_status, artifact_status),
                }
            )
            artifact_count += 1
            total_size += int(artifact.get("size") or 0)
            downloads_last_days += int(download_row.get("downloads") or 0)
            downloads_last_7_days += int(download_row.get("last_7_days") or 0)
        for row in grouped_rows.values():
            row["artifacts"] = sorted(row["artifacts"], key=lambda item: str(item["arch"]))
        packages.extend(grouped_rows.values())

    packages = sorted(packages, key=lambda row: (str(row["package_name"]).casefold(), str(row["entry_name"]).casefold()))
    all_healthy = bool(packages) and all(
        artifact["status_class"] == "ok"
        for row in packages
        for artifact in row.get("artifacts", [])
    )
    return {
        "version": 1,
        "generated_at": lock.get("generated_at"),
        "window_days": int(download_stats.get("window_days") or 30),
        "summary": {
            "entry_count": len(lock.get("packages", {})),
            "row_count": len(packages),
            "artifact_count": artifact_count,
            "total_size": total_size,
            "downloads_last_days": downloads_last_days,
            "downloads_last_7_days": downloads_last_7_days,
            "all_healthy": all_healthy,
        },
        "packages": packages,
    }


def preferred_homepage(entry: dict[str, Any], control: dict[str, Any], artifact: dict[str, Any]) -> str:
    return str(entry.get("homepage") or control.get("Homepage") or artifact.get("url") or "#")


def first_line(value: Any) -> str:
    return str(value or "").splitlines()[0].strip() if value else ""


def status_class_for(track_status: str, artifact_status: str) -> str:
    if track_status == "ok" and artifact_status == "ok":
        return "ok"
    if track_status == "kept_previous" and artifact_status == "ok":
        return "warn"
    return "bad"
