"""Known-answer tests for the request-signing primitives.

The AWS cases are the published ``aws-sig-v4-test-suite`` vectors. The Hawk,
OAuth 1.0, and EdgeGrid cases derive the expected value from the specification
algorithm written out longhand, so a refactor that changes the canonical string
fails here rather than at a server with an opaque 401.
"""

from __future__ import annotations

import base64
import hashlib
import hmac

import pytest

from api_tester.auth_signing import (
    aws_sigv4_headers,
    edgegrid_authorization,
    hawk_authorization,
    normalize_query,
    oauth1_authorization,
    percent_encode,
)

AWS_KEYS = {
    "access_key": "AKIDEXAMPLE",
    "secret_key": "wJalrXUtnFEMI/K7MDENG+bPxRfiCYEXAMPLEKEY",
    "region": "us-east-1",
    "service": "service",
    "timestamp": "20150830T123600Z",
}


def _signature(**overrides) -> str:
    headers = aws_sigv4_headers(**{**AWS_KEYS, **overrides})
    return headers["Authorization"].split("Signature=")[1]


# -- AWS SigV4 --------------------------------------------------------------


@pytest.mark.parametrize(
    "name, method, url, expected",
    [
        (
            "get-vanilla",
            "GET",
            "https://example.amazonaws.com/",
            "5fa00fa31553b73ebf1942676e86291e8372ff2a2260956d9b8aae1d763fbf31",
        ),
        (
            "get-vanilla-query-order-key-case",
            "GET",
            "https://example.amazonaws.com/?Param1=value1&Param2=value2",
            "b97d918cfa904a5beff61c982a1b6f458b799221646efd99d3219ec94cdf2500",
        ),
        (
            "post-vanilla",
            "POST",
            "https://example.amazonaws.com/",
            "5da7c1a2acd57cee7505fc6676e4e544621c30862966e37dddb68e92efbe5d6b",
        ),
    ],
)
def test_aws_sigv4_matches_the_published_test_suite(name, method, url, expected) -> None:
    assert _signature(method=method, url=url, headers={}, body=b"") == expected


def test_aws_sigv4_signs_only_host_and_date_by_default() -> None:
    headers = aws_sigv4_headers(
        method="GET", url="https://example.amazonaws.com/", headers={}, body=b"", **AWS_KEYS
    )

    # Adding x-amz-content-sha256 unconditionally would change SignedHeaders
    # and break every non-S3 service against the reference vectors.
    assert "SignedHeaders=host;x-amz-date," in headers["Authorization"]
    assert "X-Amz-Content-Sha256" not in headers


def test_aws_sigv4_can_opt_into_the_payload_header_for_s3() -> None:
    headers = aws_sigv4_headers(
        method="GET",
        url="https://example.amazonaws.com/",
        headers={},
        body=b"",
        sign_payload_header=True,
        **AWS_KEYS,
    )

    assert "x-amz-content-sha256" in headers["Authorization"]
    # The empty-body SHA-256 is a fixed, well-known constant.
    assert headers["X-Amz-Content-Sha256"] == (
        "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
    )


def test_aws_sigv4_includes_the_session_token_when_present() -> None:
    headers = aws_sigv4_headers(
        method="GET",
        url="https://example.amazonaws.com/",
        headers={},
        body=b"",
        session_token="token-value",
        **AWS_KEYS,
    )

    assert headers["X-Amz-Security-Token"] == "token-value"
    assert "x-amz-security-token" in headers["Authorization"]


def test_aws_sigv4_signature_changes_with_the_body() -> None:
    empty = _signature(method="POST", url="https://example.amazonaws.com/", headers={}, body=b"")
    filled = _signature(
        method="POST", url="https://example.amazonaws.com/", headers={}, body=b'{"a":1}'
    )

    assert empty != filled


# -- encoding helpers -------------------------------------------------------


def test_percent_encode_escapes_reserved_characters_including_slash() -> None:
    assert percent_encode("a/b c+d=e&f") == "a%2Fb%20c%2Bd%3De%26f"
    assert percent_encode("-._~") == "-._~"


def test_normalize_query_sorts_by_encoded_name_and_value() -> None:
    assert normalize_query([("b", "2"), ("a", "1"), ("a", "0")]) == "a=0&a=1&b=2"


# -- Hawk -------------------------------------------------------------------


def test_hawk_matches_the_specification_normalized_string() -> None:
    normalized = (
        "hawk.1.header\n1353832234\nj4h3g2\nGET\n/resource/1?b=1&a=2\n"
        "example.com\n8000\n\nsome-app-ext-data\n"
    )
    expected = base64.b64encode(
        hmac.new(
            b"werxhqb98rpaic9234djfpisdsdfgsdt",
            normalized.encode("utf-8"),
            hashlib.sha256,
        ).digest()
    ).decode("ascii")

    header = hawk_authorization(
        method="GET",
        url="http://example.com:8000/resource/1?b=1&a=2",
        key_id="dh37fgj492je",
        key="werxhqb98rpaic9234djfpisdsdfgsdt",
        timestamp="1353832234",
        nonce="j4h3g2",
        ext="some-app-ext-data",
    )

    assert f'mac="{expected}"' in header
    assert header.startswith("Hawk ")
    assert 'id="dh37fgj492je"' in header


def test_hawk_omits_the_payload_hash_unless_requested() -> None:
    common = {
        "method": "POST",
        "url": "https://example.com/resource",
        "key_id": "id",
        "key": "key",
        "timestamp": "1353832234",
        "nonce": "abc",
        "body": b'{"a":1}',
        "content_type": "application/json",
    }

    assert "hash=" not in hawk_authorization(**common)
    assert "hash=" in hawk_authorization(**common, include_payload_hash=True)


def test_hawk_uses_the_default_port_for_the_scheme() -> None:
    https = hawk_authorization(
        method="GET", url="https://example.com/x", key_id="i", key="k",
        timestamp="1", nonce="n",
    )
    explicit = hawk_authorization(
        method="GET", url="https://example.com:443/x", key_id="i", key="k",
        timestamp="1", nonce="n",
    )

    assert https == explicit


# -- OAuth 1.0 --------------------------------------------------------------


def test_oauth1_hmac_sha1_matches_the_specification_base_string() -> None:
    from urllib.parse import quote

    base_params = normalize_query(
        [
            ("oauth_consumer_key", "key"),
            ("oauth_nonce", "nonce"),
            ("oauth_signature_method", "HMAC-SHA1"),
            ("oauth_timestamp", "1000"),
            ("oauth_version", "1.0"),
            ("oauth_token", "token"),
            ("z", "1"),
        ]
    )
    base_string = "&".join(
        ["GET", quote("https://api.example.com/r", safe="-._~"), quote(base_params, safe="-._~")]
    )
    expected = base64.b64encode(
        hmac.new(b"cs&ts", base_string.encode("utf-8"), hashlib.sha1).digest()
    ).decode("ascii")

    header = oauth1_authorization(
        method="GET",
        url="https://api.example.com/r?z=1",
        consumer_key="key",
        consumer_secret="cs",
        token="token",
        token_secret="ts",
        timestamp="1000",
        nonce="nonce",
    )

    assert f'oauth_signature="{percent_encode(expected)}"' in header
    assert header.startswith("OAuth ")


def test_oauth1_drops_the_default_port_when_normalizing_the_url() -> None:
    common = {
        "consumer_key": "k", "consumer_secret": "cs",
        "timestamp": "1", "nonce": "n", "method": "GET",
    }

    assert oauth1_authorization(url="https://api.example.com/r", **common) == (
        oauth1_authorization(url="https://api.example.com:443/r", **common)
    )


def test_oauth1_form_body_parameters_participate_in_the_signature() -> None:
    common = {
        "method": "POST", "url": "https://api.example.com/r",
        "consumer_key": "k", "consumer_secret": "cs",
        "timestamp": "1", "nonce": "n",
    }

    assert oauth1_authorization(**common) != oauth1_authorization(
        **common, body_params=[("field", "value")]
    )


def test_oauth1_plaintext_signature_is_the_concatenated_secrets() -> None:
    header = oauth1_authorization(
        method="GET", url="https://api.example.com/r",
        consumer_key="k", consumer_secret="c s", token_secret="t s",
        signature_method="PLAINTEXT", timestamp="1", nonce="n",
    )

    assert f'oauth_signature="{percent_encode("c%20s&t%20s")}"' in header


def test_oauth1_rsa_sha1_requires_a_private_key() -> None:
    with pytest.raises(ValueError):
        oauth1_authorization(
            method="GET", url="https://api.example.com/r",
            consumer_key="k", consumer_secret="c",
            signature_method="RSA-SHA1", timestamp="1", nonce="n",
        )


# -- EdgeGrid ---------------------------------------------------------------


def test_edgegrid_signs_the_auth_header_with_a_timestamp_derived_key() -> None:
    timestamp = "20140321T19:34:21+0000"
    auth_header = (
        "EG1-HMAC-SHA256 client_token=akab-ct;access_token=akab-at;"
        f"timestamp={timestamp};nonce=nonce-xyz;"
    )
    data = (
        "GET\thttps\takaa-x.luna.akamaiapis.net\t/diagnostic/v1/locations\t\t\t"
        + auth_header
    )
    signing_key = base64.b64encode(
        hmac.new(b"cs123", timestamp.encode("utf-8"), hashlib.sha256).digest()
    ).decode("ascii")
    expected = base64.b64encode(
        hmac.new(signing_key.encode("utf-8"), data.encode("utf-8"), hashlib.sha256).digest()
    ).decode("ascii")

    header = edgegrid_authorization(
        method="GET",
        url="https://akaa-x.luna.akamaiapis.net/diagnostic/v1/locations",
        client_token="akab-ct",
        client_secret="cs123",
        access_token="akab-at",
        timestamp=timestamp,
        nonce="nonce-xyz",
    )

    assert header == f"{auth_header}signature={expected}"


def test_edgegrid_hashes_only_post_bodies() -> None:
    common = {
        "url": "https://example.akamaiapis.net/x", "client_token": "ct",
        "client_secret": "cs", "access_token": "at",
        "timestamp": "20140321T19:34:21+0000", "nonce": "n",
        "body": b'{"a":1}',
    }

    # A GET body is never hashed, so the signature matches the bodyless case.
    assert edgegrid_authorization(method="GET", **common) == edgegrid_authorization(
        **{**common, "method": "GET", "body": b""}
    )
    assert edgegrid_authorization(method="POST", **common) != edgegrid_authorization(
        **{**common, "method": "POST", "body": b""}
    )
