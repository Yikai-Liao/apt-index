from __future__ import annotations

import json
from pathlib import Path


def write_worker(path: Path, script_path: Path) -> None:
    path.write_text(script_path.read_text(encoding="utf-8"), encoding="utf-8")


def write_routes(path: Path) -> None:
    routes = {"version": 1, "include": ["/pool/*"], "exclude": []}
    path.write_text(json.dumps(routes, indent=2) + "\n", encoding="utf-8")


def write_headers(
    path: Path,
    *,
    site_data_filename: str,
    site_data_browser_ttl_policy: str,
    site_data_cdn_ttl_policy: str,
    redirect_rules_dirname: str,
    static_redirect_rules_browser_ttl_policy: str,
    static_redirect_rules_cdn_ttl_policy: str,
) -> None:
    content = "\n".join(
        [
            f"/{site_data_filename}",
            f"  Cache-Control: {site_data_browser_ttl_policy}",
            f"  Cloudflare-CDN-Cache-Control: {site_data_cdn_ttl_policy}",
            "  Content-Type: application/json; charset=utf-8",
            "",
            f"/{redirect_rules_dirname}/*.json.zst",
            f"  Cache-Control: {static_redirect_rules_browser_ttl_policy}",
            f"  Cloudflare-CDN-Cache-Control: {static_redirect_rules_cdn_ttl_policy}",
            "  Content-Type: application/json; charset=utf-8",
            "  Content-Encoding: zstd",
            "",
            f"/{redirect_rules_dirname}/*.json",
            f"  Cache-Control: {static_redirect_rules_browser_ttl_policy}",
            f"  Cloudflare-CDN-Cache-Control: {static_redirect_rules_cdn_ttl_policy}",
            "  Content-Type: application/json; charset=utf-8",
            "",
        ]
    )
    path.write_text(content, encoding="utf-8")
