"""The S3 client: Signature V4 pinned to AWS's published examples, and the
request and error behaviour pinned with a mock transport."""

from __future__ import annotations

import hashlib

import httpx
import pytest

from apify_result_cache.s3 import EMPTY_SHA256, S3Client, S3Error, sign_request, uri_encode_path

AK = "AKIAIOSFODNN7EXAMPLE"
SK = "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY"


def test_aws_example_get_object():
    """AWS S3 docs, 'Signature Calculations for the Authorization Header', Example: GET Object."""
    out = sign_request(
        method="GET", host="examplebucket.s3.amazonaws.com", path="/test.txt",
        headers={"Range": "bytes=0-9"}, payload_sha256=EMPTY_SHA256,
        access_key=AK, secret_key=SK, region="us-east-1", amz_date="20130524T000000Z",
    )
    assert out["authorization"] == (
        "AWS4-HMAC-SHA256 Credential=AKIAIOSFODNN7EXAMPLE/20130524/us-east-1/s3/aws4_request, "
        "SignedHeaders=host;range;x-amz-content-sha256;x-amz-date, "
        "Signature=f0e8bdb87c964420e857bd35b5d6ed310bd44f0170aba48dd91039c6036bdb41"
    )


def test_aws_example_put_object():
    """Same page, Example: PUT Object. Also pins the path encoding of '$'."""
    body = b"Welcome to Amazon S3."
    payload = hashlib.sha256(body).hexdigest()
    assert payload == "44ce7dd67c959e0d3524ffac1771dfbba87d2b6b4b4e99e42034a8b803f8b072"
    path = uri_encode_path("/test$file.text")
    assert path == "/test%24file.text"
    out = sign_request(
        method="PUT", host="examplebucket.s3.amazonaws.com", path=path,
        headers={"Date": "Fri, 24 May 2013 00:00:00 GMT", "x-amz-storage-class": "REDUCED_REDUNDANCY"},
        payload_sha256=payload, access_key=AK, secret_key=SK, region="us-east-1",
        amz_date="20130524T000000Z",
    )
    assert out["authorization"].endswith(
        "SignedHeaders=date;host;x-amz-content-sha256;x-amz-date;x-amz-storage-class, "
        "Signature=98ad721746da40c64f1a55b78f14c238d841ea1380cd77a1b5971af0ece108bd"
    )


ENDPOINT = "https://ref.storage.supabase.co/storage/v1/s3"


def client(handler) -> S3Client:
    return S3Client(ENDPOINT, "us-east-1", "result-cache", AK, SK, transport=httpx.MockTransport(handler))


async def test_put_sends_a_signed_path_style_request():
    seen = {}

    def handler(request: httpx.Request):
        seen["request"] = request
        return httpx.Response(200)

    body = b"\x1f\x8b payload"
    sha = hashlib.sha256(body).hexdigest()
    c = client(handler)
    await c.put_object("youtube-transcript/ab/abc.json.gz", body, sha256_hex=sha)
    await c.aclose()

    req = seen["request"]
    assert req.method == "PUT"
    assert str(req.url) == ENDPOINT + "/result-cache/youtube-transcript/ab/abc.json.gz"
    assert req.content == body
    assert req.headers["x-amz-content-sha256"] == sha
    assert req.headers["content-type"] == "application/gzip"
    # The client must sign exactly what it sends: recompute from the wire.
    expected = sign_request(
        method="PUT", host="ref.storage.supabase.co", path=req.url.raw_path.decode(),
        headers={"content-type": "application/gzip"}, payload_sha256=sha,
        access_key=AK, secret_key=SK, region="us-east-1", amz_date=req.headers["x-amz-date"],
    )
    assert req.headers["authorization"] == expected["authorization"]


async def test_get_returns_the_body_and_asks_for_identity_encoding():
    def handler(request: httpx.Request):
        assert request.headers["accept-encoding"] == "identity"
        assert request.headers["x-amz-content-sha256"] == EMPTY_SHA256
        return httpx.Response(200, content=b"blob-bytes")

    c = client(handler)
    assert await c.get_object("ns/ab/k.json.gz", max_bytes=100) == b"blob-bytes"
    await c.aclose()


@pytest.mark.parametrize("status, text", [(404, "NotFound"), (403, "HTTP 403"), (500, "HTTP 500")])
async def test_get_errors_are_reduced_to_a_code(status, text):
    c = client(lambda request: httpx.Response(status, text="<Error>secret.host.example</Error>"))
    with pytest.raises(S3Error) as exc:
        await c.get_object("ns/ab/k.json.gz", max_bytes=100)
    assert str(exc.value) == text
    await c.aclose()


async def test_get_refuses_oversized_bodies():
    c = client(lambda request: httpx.Response(200, content=b"x" * 101))
    with pytest.raises(S3Error, match="too large"):
        await c.get_object("ns/ab/k.json.gz", max_bytes=100)
    await c.aclose()


async def test_transport_errors_carry_only_the_type_name():
    def handler(request):
        raise httpx.ConnectError("could not reach ref.storage.supabase.co", request=request)

    c = client(handler)
    with pytest.raises(S3Error) as exc:
        await c.put_object("ns/ab/k.json.gz", b"x", sha256_hex=hashlib.sha256(b"x").hexdigest())
    assert str(exc.value) == "ConnectError"
    await c.aclose()


@pytest.mark.parametrize("status", [204, 200, 404])
async def test_delete_accepts_gone(status):
    c = client(lambda request: httpx.Response(status))
    await c.delete_object("ns/ab/k.json.gz")
    await c.aclose()


def test_bad_endpoint_is_rejected():
    with pytest.raises(ValueError):
        S3Client("ref.storage.supabase.co", "us-east-1", "b", AK, SK)
