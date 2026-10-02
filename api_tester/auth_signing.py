"""Pure request-signing primitives for the authentication strategies.

Every function here is deliberately free of I/O, global state, and clock
access: timestamps and nonces are always passed in. That keeps each algorithm
deterministic and testable against the published specification vectors, which
matters because a signature that is subtly wrong fails at the server with an
opaque 401 rather than anything a user could diagnose.

Implemented here rather than pulled from a library because the alternatives
(``botocore``, ``requests-oauthlib``, ``mohawk``, ``edgegrid-python``) are
heavy transitive dependencies for a PyInstaller bundle, and each algorithm is
a well-specified page of arithmetic.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
from typing import Iterable, Optional, Sequence
from urllib.parse import quote, urlsplit

__all__ = [
    "percent_encode",
    "normalize_query",
    "aws_sigv4_headers",
    "oauth1_authorization",
    "hawk_authorization",
    "edgegrid_authorization",
]

#: RFC 3986 unreserved set. ``quote`` defaults to leaving ``/`` alone, which is
#: wrong for every signature base string here, so ``safe`` is always cleared.
_UNRESERVED = "-._~"


def percent_encode(value: str) -> str:
    """RFC 3986 percent-encoding, encoding every reserved character."""
    return quote(str(value), safe=_UNRESERVED, encoding="utf-8")


def normalize_query(pairs: Iterable[tuple[str, str]]) -> str:
    """Sorts and encodes query pairs into a canonical ``a=1&b=2`` string.

    Sorting is by encoded name then encoded value, which is what SigV4 and
    OAuth 1.0 both require; sorting the raw values instead produces a
    different order whenever escaping changes the byte ordering.
    """
    encoded = sorted(
        (percent_encode(name), percent_encode(value)) for name, value in pairs
    )
    return "&".join(f"{name}={value}" for name, value in encoded)


def _hmac_sha256(key: bytes, message: str) -> bytes:
    return hmac.new(key, message.encode("utf-8"), hashlib.sha256).digest()


def _sha256_hex(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


# -- AWS Signature Version 4 ------------------------------------------------


def aws_sigv4_headers(
    *,
    method: str,
    url: str,
    headers: dict[str, str],
    body: bytes,
    access_key: str,
    secret_key: str,
    region: str,
    service: str,
    timestamp: str,
    session_token: str = "",
    sign_payload_header: bool = False,
) -> dict[str, str]:
    """Builds the SigV4 ``Authorization`` header and its required companions.

    ``timestamp`` is an ISO basic UTC stamp (``YYYYMMDDTHHMMSSZ``). The caller
    supplies it so the signature is reproducible in tests.

    ``sign_payload_header`` adds ``x-amz-content-sha256`` to the signed set.
    S3 requires it; most other services do not, and including it
    unconditionally would change ``SignedHeaders`` and invalidate the
    signature against the AWS reference test suite.
    """
    split = urlsplit(url)
    date = timestamp[:8]
    payload_hash = _sha256_hex(body or b"")

    # x-amz-date and the host header are part of what gets signed, so they are
    # injected before the canonical header block is built rather than after.
    signed_headers_map = {
        name.lower(): " ".join(str(value).split())
        for name, value in headers.items()
        if name.lower() not in {"authorization", "content-length"}
    }
    signed_headers_map["host"] = split.netloc
    signed_headers_map["x-amz-date"] = timestamp
    if sign_payload_header:
        signed_headers_map["x-amz-content-sha256"] = payload_hash
    if session_token:
        signed_headers_map["x-amz-security-token"] = session_token

    ordered = sorted(signed_headers_map)
    canonical_headers = "".join(f"{name}:{signed_headers_map[name]}\n" for name in ordered)
    signed_headers = ";".join(ordered)

    query_pairs: list[tuple[str, str]] = []
    if split.query:
        for chunk in split.query.split("&"):
            if not chunk:
                continue
            name, _, value = chunk.partition("=")
            query_pairs.append((name, value))
    canonical_query = normalize_query(query_pairs)

    canonical_request = "\n".join(
        [
            method.upper(),
            quote(split.path or "/", safe="/-._~"),
            canonical_query,
            canonical_headers,
            signed_headers,
            payload_hash,
        ]
    )
    scope = f"{date}/{region}/{service}/aws4_request"
    string_to_sign = "\n".join(
        [
            "AWS4-HMAC-SHA256",
            timestamp,
            scope,
            _sha256_hex(canonical_request.encode("utf-8")),
        ]
    )
    signing_key = _hmac_sha256(f"AWS4{secret_key}".encode("utf-8"), date)
    for part in (region, service, "aws4_request"):
        signing_key = _hmac_sha256(signing_key, part)
    signature = hmac.new(
        signing_key, string_to_sign.encode("utf-8"), hashlib.sha256
    ).hexdigest()

    result = {
        "Authorization": (
            f"AWS4-HMAC-SHA256 Credential={access_key}/{scope}, "
            f"SignedHeaders={signed_headers}, Signature={signature}"
        ),
        "X-Amz-Date": timestamp,
    }
    if sign_payload_header:
        result["X-Amz-Content-Sha256"] = payload_hash
    if session_token:
        result["X-Amz-Security-Token"] = session_token
    return result


# -- OAuth 1.0 --------------------------------------------------------------


def oauth1_authorization(
    *,
    method: str,
    url: str,
    consumer_key: str,
    consumer_secret: str,
    token: str = "",
    token_secret: str = "",
    signature_method: str = "HMAC-SHA1",
    timestamp: str,
    nonce: str,
    realm: str = "",
    body_params: Optional[Sequence[tuple[str, str]]] = None,
    private_key: str = "",
    extra_params: Optional[Sequence[tuple[str, str]]] = None,
) -> str:
    """Builds an OAuth 1.0a ``Authorization: OAuth ...`` header value.

    ``body_params`` must be supplied only for a form-encoded body; per the
    specification those participate in the signature base string, while a JSON
    or binary body does not.
    """
    split = urlsplit(url)
    oauth_params: list[tuple[str, str]] = [
        ("oauth_consumer_key", consumer_key),
        ("oauth_nonce", nonce),
        ("oauth_signature_method", signature_method),
        ("oauth_timestamp", timestamp),
        ("oauth_version", "1.0"),
    ]
    if token:
        oauth_params.append(("oauth_token", token))
    if extra_params:
        oauth_params.extend(extra_params)

    signature_base_params = list(oauth_params)
    if split.query:
        for chunk in split.query.split("&"):
            if not chunk:
                continue
            name, _, value = chunk.partition("=")
            # Query values arrive already encoded; decode so they are not
            # double-encoded when the base string is assembled.
            from urllib.parse import unquote

            signature_base_params.append((unquote(name), unquote(value)))
    if body_params:
        signature_base_params.extend(body_params)

    # The port is dropped when it is the scheme default, per RFC 5849 3.4.1.2.
    scheme = split.scheme.lower()
    host = (split.hostname or "").lower()
    port = split.port
    if port and not ((scheme == "http" and port == 80) or (scheme == "https" and port == 443)):
        host = f"{host}:{port}"
    normalized_url = f"{scheme}://{host}{split.path or '/'}"

    if signature_method == "PLAINTEXT":
        signature = f"{percent_encode(consumer_secret)}&{percent_encode(token_secret)}"
    else:
        base_string = "&".join(
            [
                method.upper(),
                percent_encode(normalized_url),
                percent_encode(normalize_query(signature_base_params)),
            ]
        )
        if signature_method == "RSA-SHA1":
            signature = _rsa_sha1_signature(base_string, private_key)
        else:
            signing_key = (
                f"{percent_encode(consumer_secret)}&{percent_encode(token_secret)}"
            ).encode("utf-8")
            digest = hmac.new(
                signing_key,
                base_string.encode("utf-8"),
                hashlib.sha256 if signature_method == "HMAC-SHA256" else hashlib.sha1,
            ).digest()
            signature = base64.b64encode(digest).decode("ascii")

    header_params = list(oauth_params) + [("oauth_signature", signature)]
    if realm:
        header_params.insert(0, ("realm", realm))
    rendered = ", ".join(
        f'{percent_encode(name)}="{percent_encode(value)}"' for name, value in header_params
    )
    return f"OAuth {rendered}"


def _rsa_sha1_signature(base_string: str, private_key: str) -> str:
    if not private_key.strip():
        raise ValueError("RSA-SHA1 requires a PEM private key.")
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import padding

    key = serialization.load_pem_private_key(private_key.encode("utf-8"), password=None)
    signature = key.sign(base_string.encode("utf-8"), padding.PKCS1v15(), hashes.SHA1())
    return base64.b64encode(signature).decode("ascii")


# -- Hawk -------------------------------------------------------------------


def hawk_authorization(
    *,
    method: str,
    url: str,
    key_id: str,
    key: str,
    timestamp: str,
    nonce: str,
    algorithm: str = "sha256",
    ext: str = "",
    body: bytes = b"",
    content_type: str = "",
    include_payload_hash: bool = False,
) -> str:
    """Builds a Hawk ``Authorization`` header value."""
    digest = hashlib.sha256 if algorithm == "sha256" else hashlib.sha1
    split = urlsplit(url)
    port = split.port or (443 if split.scheme == "https" else 80)
    resource = split.path or "/"
    if split.query:
        resource = f"{resource}?{split.query}"

    payload_hash = ""
    if include_payload_hash:
        normalized_type = content_type.split(";")[0].strip().lower()
        payload_normalized = (
            f"hawk.1.payload\n{normalized_type}\n{body.decode('utf-8', 'replace')}\n"
        )
        payload_hash = base64.b64encode(
            digest(payload_normalized.encode("utf-8")).digest()
        ).decode("ascii")

    normalized = (
        "hawk.1.header\n"
        f"{timestamp}\n{nonce}\n{method.upper()}\n{resource}\n"
        f"{(split.hostname or '').lower()}\n{port}\n{payload_hash}\n{ext}\n"
    )
    mac = base64.b64encode(
        hmac.new(key.encode("utf-8"), normalized.encode("utf-8"), digest).digest()
    ).decode("ascii")

    parts = [f'id="{key_id}"', f'ts="{timestamp}"', f'nonce="{nonce}"']
    if payload_hash:
        parts.append(f'hash="{payload_hash}"')
    if ext:
        parts.append(f'ext="{ext}"')
    parts.append(f'mac="{mac}"')
    return "Hawk " + ", ".join(parts)


# -- Akamai EdgeGrid --------------------------------------------------------


def edgegrid_authorization(
    *,
    method: str,
    url: str,
    client_token: str,
    client_secret: str,
    access_token: str,
    timestamp: str,
    nonce: str,
    body: bytes = b"",
    max_body: int = 131072,
) -> str:
    """Builds an Akamai EdgeGrid ``Authorization`` header value.

    ``timestamp`` uses EdgeGrid's own format, ``YYYYMMDDTHH:MM:SS+0000``.
    """
    split = urlsplit(url)
    resource = split.path or "/"
    if split.query:
        resource = f"{resource}?{split.query}"

    # Only POST bodies are hashed, and only up to max_body, per the spec.
    content_hash = ""
    if method.upper() == "POST" and body:
        content_hash = base64.b64encode(
            hashlib.sha256(body[:max_body]).digest()
        ).decode("ascii")

    auth_header = (
        f"EG1-HMAC-SHA256 client_token={client_token};"
        f"access_token={access_token};"
        f"timestamp={timestamp};"
        f"nonce={nonce};"
    )
    data_to_sign = (
        f"{method.upper()}\t{split.scheme}\t{split.netloc}\t{resource}\t\t"
        f"{content_hash}\t{auth_header}"
    )
    signing_key = base64.b64encode(
        _hmac_sha256(client_secret.encode("utf-8"), timestamp)
    ).decode("ascii")
    signature = base64.b64encode(
        _hmac_sha256(signing_key.encode("utf-8"), data_to_sign)
    ).decode("ascii")
    return f"{auth_header}signature={signature}"
