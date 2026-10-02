"""A minimal S3 client: path-style requests signed with AWS Signature V4, over httpx.

Why not boto3: it adds ~90 MB to an Actor image and a noticeable import cost to
every run, and this library is meant for the whole fleet, including Actors
with a 128 MB memory cap. Three operations on single small objects need about
eighty lines of signing, which the tests pin against AWS's published examples.

Works with any S3-compatible endpoint that accepts path-style requests: the
Supabase Storage S3 endpoint today, a self-hosted server later (Phase 2).
"""

from __future__ import annotations

import datetime
import hashlib
import hmac
from typing import Mapping
from urllib.parse import quote, urlsplit

import httpx

EMPTY_SHA256 = hashlib.sha256(b"").hexdigest()


class S3Error(RuntimeError):
    """Any storage failure. The text is a type name or `HTTP <status>`, never a URL or key."""


def _hmac(key: bytes, msg: str) -> bytes:
    return hmac.new(key, msg.encode("utf-8"), hashlib.sha256).digest()


def uri_encode_path(path: str) -> str:
    """S3 canonical URI encoding: every byte except unreserved characters and '/'."""
    return quote(path, safe="/-_.~")


def sign_request(
    *,
    method: str,
    host: str,
    path: str,
    payload_sha256: str,
    access_key: str,
    secret_key: str,
    region: str,
    amz_date: str,
    headers: Mapping[str, str] | None = None,
    query: Mapping[str, str] | None = None,
    service: str = "s3",
) -> dict[str, str]:
    """Signature V4 for one request. `path` must already be URI-encoded.

    Returns the three headers to send alongside `headers`: x-amz-date,
    x-amz-content-sha256 and authorization. Every header in `headers` is signed,
    so send exactly those.
    """
    signed = {k.lower().strip(): " ".join(str(v).strip().split()) for k, v in (headers or {}).items()}
    signed["host"] = host
    signed["x-amz-content-sha256"] = payload_sha256
    signed["x-amz-date"] = amz_date
    names = sorted(signed)
    canonical_headers = "".join(f"{name}:{signed[name]}\n" for name in names)
    signed_headers = ";".join(names)
    canonical_query = "&".join(
        f"{quote(str(k), safe='-_.~')}={quote(str(v), safe='-_.~')}"
        for k, v in sorted((query or {}).items())
    )
    canonical_request = "\n".join(
        [method.upper(), path, canonical_query, canonical_headers, signed_headers, payload_sha256]
    )
    date = amz_date[:8]
    scope = f"{date}/{region}/{service}/aws4_request"
    string_to_sign = "\n".join(
        ["AWS4-HMAC-SHA256", amz_date, scope, hashlib.sha256(canonical_request.encode("utf-8")).hexdigest()]
    )
    key = _hmac(("AWS4" + secret_key).encode("utf-8"), date)
    key = _hmac(key, region)
    key = _hmac(key, service)
    key = _hmac(key, "aws4_request")
    signature = hmac.new(key, string_to_sign.encode("utf-8"), hashlib.sha256).hexdigest()
    return {
        "x-amz-date": amz_date,
        "x-amz-content-sha256": payload_sha256,
        "authorization": (
            f"AWS4-HMAC-SHA256 Credential={access_key}/{scope}, "
            f"SignedHeaders={signed_headers}, Signature={signature}"
        ),
    }


class S3Client:
    """PUT, GET and DELETE of single objects in one bucket. Never retries, never logs."""

    def __init__(
        self,
        endpoint: str,
        region: str,
        bucket: str,
        access_key: str,
        secret_key: str,
        *,
        timeout: float = 5.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        parts = urlsplit(endpoint.strip().rstrip("/"))
        if parts.scheme not in ("https", "http") or not parts.netloc:
            raise ValueError("storage endpoint must be an http(s) URL")
        self._scheme = parts.scheme
        self._host = parts.netloc
        self._prefix = parts.path.rstrip("/")
        self._region = region
        self._bucket = bucket
        self._access_key = access_key
        self._secret_key = secret_key
        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(connect=2.0, read=timeout, write=timeout, pool=2.0),
            transport=transport,
        )

    def _path(self, key: str) -> str:
        return uri_encode_path(f"{self._prefix}/{self._bucket}/{key}")

    async def _send(
        self,
        method: str,
        key: str,
        body: bytes = b"",
        payload_sha256: str = EMPTY_SHA256,
        headers: Mapping[str, str] | None = None,
    ) -> httpx.Response:
        path = self._path(key)
        amz_date = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        send_headers = dict(headers or {})
        send_headers.update(sign_request(
            method=method, host=self._host, path=path, payload_sha256=payload_sha256,
            access_key=self._access_key, secret_key=self._secret_key, region=self._region,
            amz_date=amz_date, headers=headers,
        ))
        try:
            return await self._client.request(
                method, f"{self._scheme}://{self._host}{path}",
                content=body or None, headers=send_headers,
            )
        except Exception as exc:  # noqa: BLE001 - type name only; the message can carry the host
            raise S3Error(type(exc).__name__) from None

    async def put_object(self, key: str, body: bytes, *, sha256_hex: str,
                         content_type: str = "application/gzip") -> None:
        response = await self._send("PUT", key, body, sha256_hex, {"content-type": content_type})
        if response.status_code not in (200, 201):
            raise S3Error(f"HTTP {response.status_code}")

    async def get_object(self, key: str, *, max_bytes: int) -> bytes:
        # identity: the object is already gzip; a transparent decode would break the digest check.
        response = await self._send("GET", key, headers={"accept-encoding": "identity"})
        if response.status_code == 404:
            raise S3Error("NotFound")
        if response.status_code != 200:
            raise S3Error(f"HTTP {response.status_code}")
        body = response.content
        if len(body) > max_bytes:
            raise S3Error("too large")
        return body

    async def delete_object(self, key: str) -> None:
        response = await self._send("DELETE", key)
        if response.status_code not in (200, 204, 404):
            raise S3Error(f"HTTP {response.status_code}")

    async def aclose(self) -> None:
        try:
            await self._client.aclose()
        except Exception:  # noqa: BLE001 - closing must never break a run
            pass
