"""The payload codec: same dict in, same bytes out, and nothing tampered gets through."""

from __future__ import annotations

import gzip
import hashlib
from datetime import datetime, timezone

import pytest

from apify_result_cache import CodecError
from apify_result_cache import codec

PAYLOAD = {"video_id": "abc", "timestamped": [{"text": "héllo", "start": 0.0, "duration": 1.5}],
           "language": "en", "n": 3}


def test_roundtrip():
    blob, sha, size = codec.encode(PAYLOAD)
    assert size == len(blob)
    assert sha == hashlib.sha256(blob).hexdigest()
    assert codec.decode(blob, sha) == PAYLOAD


def test_deterministic_regardless_of_key_order():
    a = codec.encode({"b": 1, "a": [1, 2]})
    b = codec.encode({"a": [1, 2], "b": 1})
    assert a == b


def test_sha_mismatch_is_rejected():
    blob, sha, _ = codec.encode(PAYLOAD)
    with pytest.raises(CodecError, match="sha256"):
        codec.decode(blob, "0" * 64)


def test_corrupt_gzip_is_rejected():
    blob = b"not gzip at all"
    with pytest.raises(CodecError):
        codec.decode(blob, hashlib.sha256(blob).hexdigest())


def test_truncated_gzip_is_rejected():
    blob, _, _ = codec.encode(PAYLOAD)
    cut = blob[:-6]
    with pytest.raises(CodecError):
        codec.decode(cut, hashlib.sha256(cut).hexdigest())


def test_non_object_json_is_rejected():
    blob = gzip.compress(b"[1,2,3]", mtime=0)
    with pytest.raises(CodecError, match="object"):
        codec.decode(blob, hashlib.sha256(blob).hexdigest())


def test_bomb_guard():
    big = gzip.compress(b"0" * (codec.MAX_DECODED_BYTES + 10), compresslevel=9, mtime=0)
    assert len(big) < 100_000  # it compresses to almost nothing, which is the point
    with pytest.raises(CodecError, match="large"):
        codec.decode(big, hashlib.sha256(big).hexdigest())


def test_unserialisable_payload_is_rejected():
    with pytest.raises(CodecError):
        codec.encode({"x": object()})


def test_payload_must_be_a_dict():
    with pytest.raises(CodecError):
        codec.encode([1, 2])  # type: ignore[arg-type]


def test_blob_ref_layout():
    ref = codec.blob_ref("youtube-transcript", "ab" * 32, datetime(2026, 10, 5, tzinfo=timezone.utc))
    assert ref == "youtube-transcript/2026/10/" + "ab" * 32 + ".json.gz"


def test_non_ascii_survives():
    blob, sha, _ = codec.encode({"t": "日本語 – ñ"})
    assert codec.decode(blob, sha)["t"] == "日本語 – ñ"
