"""The payload codec: same dict in, same bytes out, and nothing tampered gets through."""

from __future__ import annotations

import gzip
import json
import hashlib
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


def test_deterministic_for_the_same_payload():
    assert codec.encode({"b": 1, "a": [1, 2]}) == codec.encode({"b": 1, "a": [1, 2]})


def test_key_order_is_preserved_through_a_roundtrip():
    """A served result must read exactly like the fresh one, nested dicts included."""
    payload = {"video_id": "v", "timestamped": [{"text": "t", "start": 1.0, "duration": 2.0}], "a": 1}
    blob, sha, _ = codec.encode(payload)
    decoded = codec.decode(blob, sha)
    assert list(decoded) == ["video_id", "timestamped", "a"]
    assert list(decoded["timestamped"][0]) == ["text", "start", "duration"]
    assert json.dumps(decoded) == json.dumps(payload)


def test_keys_still_hash_sorted():
    assert codec.canonical_json({"b": 1, "a": 2}) == codec.canonical_json({"a": 2, "b": 1})


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


def test_blob_ref_layout_is_stable_per_key():
    key = "cd" + "ab" * 31
    assert codec.blob_ref("youtube-transcript", key) == f"youtube-transcript/cd/{key}.json.gz"
    assert codec.blob_ref("youtube-transcript", key) == codec.blob_ref("youtube-transcript", key)


def test_non_ascii_survives():
    blob, sha, _ = codec.encode({"t": "日本語 – ñ"})
    assert codec.decode(blob, sha)["t"] == "日本語 – ñ"
