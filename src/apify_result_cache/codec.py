"""Payload bytes: canonical JSON -> gzip -> sha256, and the way back.

Pure functions. This is the one module that raises on purpose, because a
corrupt or tampered blob must never become a dataset row; the cache turns a
`CodecError` into a miss.
"""

from __future__ import annotations

import gzip
import hashlib
import hmac
import json
import zlib
from datetime import datetime
from typing import Any


class CodecError(ValueError):
    """The bytes are not a payload we wrote. Treated as a miss upstream."""


COMPRESS_LEVEL = 6
MAX_DECODED_BYTES = 16 * 1024 * 1024  # bomb guard; the largest real payload is ~4 MB


def canonical_json(obj: Any) -> bytes:
    """Sorted keys, no whitespace, UTF-8. Same dict -> same bytes, always."""
    try:
        return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise CodecError(f"not JSON-serialisable ({type(exc).__name__})") from None


def encode(payload: dict[str, Any]) -> tuple[bytes, str, int]:
    """Returns (blob, sha256 hex of the blob, size in bytes).

    `mtime=0` makes the gzip header deterministic, so the same payload always
    produces the same bytes and the same digest.
    """
    if not isinstance(payload, dict):
        raise CodecError("payload must be a dict")
    blob = gzip.compress(canonical_json(payload), compresslevel=COMPRESS_LEVEL, mtime=0)
    return blob, hashlib.sha256(blob).hexdigest(), len(blob)


def decode(blob: bytes, expected_sha256: str) -> dict[str, Any]:
    """Verify the digest, inflate within the size cap, parse. Raises CodecError."""
    if not isinstance(blob, (bytes, bytearray)) or not blob:
        raise CodecError("empty blob")
    actual = hashlib.sha256(blob).hexdigest()
    if not hmac.compare_digest(actual, (expected_sha256 or "").strip().lower()):
        raise CodecError("sha256 mismatch")

    inflater = zlib.decompressobj(16 + zlib.MAX_WBITS)
    try:
        raw = inflater.decompress(bytes(blob), MAX_DECODED_BYTES + 1)
    except zlib.error as exc:
        raise CodecError(f"corrupt gzip ({type(exc).__name__})") from None
    if len(raw) > MAX_DECODED_BYTES or inflater.unconsumed_tail:
        raise CodecError("payload too large")
    if not inflater.eof:
        raise CodecError("truncated gzip")

    try:
        obj = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        raise CodecError("bad payload JSON") from None
    if not isinstance(obj, dict):
        raise CodecError("payload is not an object")
    return obj


def blob_ref(namespace: str, key_hash: str, when: datetime) -> str:
    """Object path in the bucket. Month-prefixed so expiry deletes stay cheap."""
    return f"{namespace}/{when:%Y}/{when:%m}/{key_hash}.json.gz"
