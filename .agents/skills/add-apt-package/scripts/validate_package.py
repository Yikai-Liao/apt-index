#!/usr/bin/env python3

from __future__ import annotations

import argparse
import json
from pathlib import Path
from urllib.request import Request, urlopen

from apt_index import sources
from apt_index.config import load_configuration


def fetch_json(url: str, headers: dict[str, str] | None):
    request = Request(url, headers=headers or {})
    with urlopen(request) as response:
        return json.load(response)


def fetch_text(url: str, headers: dict[str, str] | None):
    request = Request(url, headers=headers or {})
    with urlopen(request) as response:
        return response.read().decode()


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Load one apt-index package entry and resolve every declared architecture."
    )
    parser.add_argument("package", help="Flat package entry name, for example ripgrep or teams-for-linux.")
    parser.add_argument(
        "--root",
        default=".",
        help="Repository root containing apt-index.toml and packages/. Defaults to the current directory.",
    )
    args = parser.parse_args()

    root = Path(args.root).resolve()
    config = load_configuration(root)
    entry = config.packages[args.package]

    print("ENTRY", json.dumps(entry.model_dump(mode="json"), ensure_ascii=True, sort_keys=True))

    for architecture, arch_config in entry.architectures.items():
        candidate = sources.resolve_candidate(
            arch_config.model_dump(mode="json"),
            fetch_json=fetch_json,
            fetch_text=fetch_text,
            root=root,
        )
        print(
            "CANDIDATE",
            json.dumps(
                {
                    "architecture": architecture,
                    "asset_name": candidate.asset_name,
                    "url": candidate.url,
                    "version": candidate.upstream_version,
                    "checksum_algorithm": candidate.hash_algorithm if candidate.expected_hash else None,
                },
                ensure_ascii=True,
                sort_keys=True,
            ),
        )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
