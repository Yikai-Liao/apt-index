from __future__ import annotations

import json
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict


class JsonFiles:
    def load(self, path: Path, default: Any = None) -> Any:
        if not path.exists():
            return default
        return json.loads(path.read_text(encoding="utf-8"))

    def write(self, path: Path, data: Any) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")


class SystemClock:
    def utc_now(self) -> datetime:
        return datetime.now(timezone.utc)

    def now_iso(self) -> str:
        return self.utc_now().replace(microsecond=0).isoformat()

    def graphql_time(self, value: datetime) -> str:
        return value.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


class UrlLibHttpClient(BaseModel):
    model_config = ConfigDict(frozen=True)

    user_agent: str

    def fetch_json(self, url: str, headers: dict[str, str] | None = None) -> Any:
        return json.loads(self.fetch_text(url, headers))

    def fetch_bytes(self, url: str, headers: dict[str, str] | None = None) -> bytes:
        request_headers = {"User-Agent": self.user_agent}
        request_headers.update(headers or {})
        request = urllib.request.Request(url, headers=request_headers)
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                return response.read()
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"GET {url} failed: HTTP {exc.code}: {body}") from exc

    def fetch_text(self, url: str, headers: dict[str, str] | None = None) -> str:
        return self.fetch_bytes(url, headers).decode("utf-8")

    def post_json(self, url: str, payload: Any, headers: dict[str, str] | None = None) -> Any:
        return json.loads(self.post_text(url, json.dumps(payload), headers))

    def post_text(self, url: str, body: str, headers: dict[str, str] | None = None) -> str:
        request_headers = {"User-Agent": self.user_agent}
        request_headers.update(headers or {})
        request = urllib.request.Request(url, data=body.encode("utf-8"), headers=request_headers, method="POST")
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                return response.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            response_body = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"POST {url} failed: HTTP {exc.code}: {response_body}") from exc
