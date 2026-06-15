from __future__ import annotations

import io

import zstandard


def compress(data: bytes, *, level: int = 19) -> bytes:
    return zstandard.ZstdCompressor(level=level).compress(data)


def decompress(data: bytes) -> bytes:
    try:
        with zstandard.ZstdDecompressor().stream_reader(io.BytesIO(data)) as reader:
            return reader.read()
    except zstandard.ZstdError as exc:
        raise RuntimeError("invalid zstd data") from exc
