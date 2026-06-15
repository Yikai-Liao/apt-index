#!/usr/bin/env python3

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
from urllib.request import Request, urlopen


def fetch_latest_release(repo: str) -> dict[str, object]:
    path = f"repos/{repo}/releases/latest"
    gh = shutil.which("gh")
    if gh:
        result = subprocess.run(
            [gh, "api", path],
            check=True,
            text=True,
            capture_output=True,
        )
        return json.loads(result.stdout)

    request = Request(f"https://api.github.com/{path}")
    with urlopen(request) as response:
        return json.load(response)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Print the latest GitHub release and its assets, defaulting to .deb assets only."
    )
    parser.add_argument("repo", help="GitHub repo in owner/name form.")
    parser.add_argument(
        "--all-assets",
        action="store_true",
        help="Print every asset instead of filtering to .deb files.",
    )
    args = parser.parse_args()

    release = fetch_latest_release(args.repo)
    assets = [asset["name"] for asset in release.get("assets", [])]
    if not args.all_assets:
        assets = [name for name in assets if name.endswith(".deb")]

    print(
        json.dumps(
            {
                "repo": args.repo,
                "tag_name": release.get("tag_name"),
                "prerelease": release.get("prerelease"),
                "draft": release.get("draft"),
                "assets": assets,
            },
            ensure_ascii=True,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
