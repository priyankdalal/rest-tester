"""Tests for the generic (non-Microsoft) OAuth 2.0 client.

A fake transport stands in for the provider so the grants, error mapping and
client-authentication placement are asserted without a live server.
"""

from __future__ import annotations

import base64
import time
import urllib.parse

import pytest

from api_tester import oauth2_client as oc


class FakeHTTP:
    """Records token requests and replays queued responses."""

    def __init__(self, *responses) -> None:
        self.responses = list(responses)
        self.calls: list[dict] = []

    def post(self, url, data=None, headers=None, timeout=None):
        self.calls.append({"url": url, "data": dict(data or {}), "headers": dict(headers or {})})
        status, payload = self.responses.pop(0)
        return _Response(status, payload)


class _Response:
    def __init__(self, status_code, payload) -> None:
        self.status_code = status_code
        self._payload = payload

    def json(self):
        if self._payload is None:
            raise ValueError("not json")
        return self._payload


def config(**overrides) -> oc.OAuth2Config:
    base = dict(
        client_id="app-1",
        token_url="https://id.example.test/token",
        authorization_url="https://id.example.test/authorize",
        device_authorization_url="https://id.example.test/device",
        client_secret="sh-secret",
        scopes=("read", "write"),
    )
    base.update(overrides)
    return oc.OAuth2Config(**base)


# -- PKCE -------------------------------------------------------------------


def test_pkce_challenge_is_unpadded_base64url_sha256() -> None:
    verifier, challenge = oc.generate_pkce_pair(verifier_factory=lambda: "a" * 64)
    import hashlib

    expected = base64.urlsafe_b64encode(
        hashlib.sha256(("a" * 64).encode("ascii")).digest()
    ).decode("ascii").rstrip("=")
    assert challenge == expected
    assert "=" not in challenge
    assert verifier == "a" * 64


# -- client authentication placement ---------------------------------------


def test_confidential_client_defaults_to_a_basic_header() -> None:
    http = FakeHTTP((200, {"access_token": "t", "expires_in": 3600}))
    oc.request_client_credentials(config(), session=http)

    sent = http.calls[0]
    scheme, encoded = sent["headers"]["Authorization"].split(" ", 1)
    assert scheme == "Basic"
    assert base64.b64decode(encoded).decode("utf-8") == "app-1:sh-secret"
    assert "client_secret" not in sent["data"]


def test_client_secret_can_be_sent_in_the_body_instead() -> None:
    http = FakeHTTP((200, {"access_token": "t"}))
    oc.request_client_credentials(config(client_auth="body"), session=http)

    sent = http.calls[0]
    assert "Authorization" not in sent["headers"]
    assert sent["data"]["client_secret"] == "sh-secret"
    assert sent["data"]["client_id"] == "app-1"


def test_public_client_identifies_itself_without_a_secret() -> None:
    http = FakeHTTP((200, {"access_token": "t"}))
    oc.request_client_credentials(config(client_secret=""), session=http)

    sent = http.calls[0]
    assert "Authorization" not in sent["headers"]
    assert sent["data"] == {"client_id": "app-1", "grant_type": "client_credentials",
                            "scope": "read write"}


def test_basic_header_percent_encodes_credentials() -> None:
    """RFC 6749 requires form-urlencoding before base64 for these values."""
    http = FakeHTTP((200, {"access_token": "t"}))
    oc.request_client_credentials(
        config(client_id="app:1", client_secret="p@ss word"), session=http
    )
    encoded = http.calls[0]["headers"]["Authorization"].split(" ", 1)[1]
    assert base64.b64decode(encoded).decode("utf-8") == "app%3A1:p%40ss%20word"


# -- grants -----------------------------------------------------------------


def test_client_credentials_grant_sends_scope_and_audience() -> None:
    http = FakeHTTP((200, {"access_token": "t", "expires_in": 60}))
    oc.request_client_credentials(config(audience="https://api.example.test"), session=http)

    data = http.calls[0]["data"]
    assert data["grant_type"] == "client_credentials"
    assert data["scope"] == "read write"
    assert data["audience"] == "https://api.example.test"


def test_password_grant_passes_the_resource_owner_credentials() -> None:
    http = FakeHTTP((200, {"access_token": "t"}))
    oc.request_password_token(config(), "alice", "s3cr3t", session=http)

    data = http.calls[0]["data"]
    assert data["grant_type"] == "password"
    assert (data["username"], data["password"]) == ("alice", "s3cr3t")


def test_authorization_code_exchange_sends_the_verifier() -> None:
    http = FakeHTTP((200, {"access_token": "t", "refresh_token": "r"}))
    token = oc.exchange_authorization_code(
        config(), "the-code",
        redirect_uri="http://localhost:7777", code_verifier="the-verifier",
        session=http,
    )

    data = http.calls[0]["data"]
    assert data["grant_type"] == "authorization_code"
    assert data["code"] == "the-code"
    assert data["code_verifier"] == "the-verifier"
    assert data["redirect_uri"] == "http://localhost:7777"
    assert token.refresh_token == "r"


def test_refresh_keeps_the_old_token_when_the_provider_does_not_rotate() -> None:
    http = FakeHTTP((200, {"access_token": "new", "expires_in": 3600}))
    token = oc.refresh_access_token(config(), "original-refresh", session=http)

    assert token.access_token == "new"
    assert token.refresh_token == "original-refresh"


def test_refresh_adopts_a_rotated_refresh_token() -> None:
    http = FakeHTTP((200, {"access_token": "new", "refresh_token": "rotated"}))
    token = oc.refresh_access_token(config(), "original-refresh", session=http)
    assert token.refresh_token == "rotated"


# -- authorization URL ------------------------------------------------------


def test_authorization_url_carries_pkce_and_state() -> None:
    url = oc.build_authorization_url(
        config(), redirect_uri="http://localhost:7777",
        state="the-state", code_challenge="the-challenge",
    )
    query = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(url).query))
    assert query["response_type"] == "code"
    assert query["code_challenge_method"] == "S256"
    assert query["code_challenge"] == "the-challenge"
    assert query["state"] == "the-state"
    assert query["redirect_uri"] == "http://localhost:7777"


def test_authorization_url_appends_to_an_existing_query_string() -> None:
    url = oc.build_authorization_url(
        config(authorization_url="https://id.example.test/authorize?tenant=x"),
        redirect_uri="http://localhost", state="s", code_challenge="c",
    )
    assert "/authorize?tenant=x&" in url


# -- device code ------------------------------------------------------------


def test_device_flow_polls_until_the_user_approves() -> None:
    http = FakeHTTP(
        (400, {"error": "authorization_pending"}),
        (400, {"error": "authorization_pending"}),
        (200, {"access_token": "granted"}),
    )
    slept: list[float] = []
    token = oc.poll_device_token(
        config(),
        {"device_code": "dc", "interval": 5, "expires_in": 600},
        session=http, sleep=slept.append, clock=lambda: 0.0,
    )
    assert token.access_token == "granted"
    assert slept == [5, 5]


def test_device_flow_backs_off_when_told_to_slow_down() -> None:
    http = FakeHTTP(
        (400, {"error": "slow_down"}),
        (200, {"access_token": "granted"}),
    )
    slept: list[float] = []
    oc.poll_device_token(
        config(), {"device_code": "dc", "interval": 5, "expires_in": 600},
        session=http, sleep=slept.append, clock=lambda: 0.0,
    )
    assert slept == [10]


def test_device_flow_gives_up_when_the_code_expires() -> None:
    http = FakeHTTP((400, {"error": "authorization_pending"}))
    ticks = iter([0.0, 1000.0, 2000.0])
    with pytest.raises(oc.OAuth2InteractionRequired, match="expired"):
        oc.poll_device_token(
            config(), {"device_code": "dc", "interval": 1, "expires_in": 1},
            session=http, sleep=lambda _: None, clock=lambda: next(ticks),
        )


def test_device_flow_surfaces_a_real_denial_immediately() -> None:
    http = FakeHTTP((400, {"error": "access_denied", "error_description": "user said no"}))
    with pytest.raises(oc.OAuth2Error, match="access_denied: user said no"):
        oc.poll_device_token(
            config(), {"device_code": "dc", "interval": 1, "expires_in": 600},
            session=http, sleep=lambda _: None, clock=lambda: 0.0,
        )


# -- errors -----------------------------------------------------------------


def test_provider_errors_keep_the_code_and_description() -> None:
    http = FakeHTTP((400, {"error": "invalid_client", "error_description": "bad secret"}))
    with pytest.raises(oc.OAuth2Error, match="invalid_client: bad secret"):
        oc.request_client_credentials(config(), session=http)


def test_interaction_errors_are_distinguished_from_hard_failures() -> None:
    http = FakeHTTP((400, {"error": "interaction_required"}))
    with pytest.raises(oc.OAuth2InteractionRequired):
        oc.request_client_credentials(config(), session=http)


def test_a_non_json_response_is_reported_rather_than_crashing() -> None:
    http = FakeHTTP((502, None))
    with pytest.raises(oc.OAuth2Error, match="non-JSON response"):
        oc.request_client_credentials(config(), session=http)


def test_a_missing_access_token_is_an_error_not_an_empty_credential() -> None:
    http = FakeHTTP((200, {"token_type": "Bearer"}))
    with pytest.raises(oc.OAuth2Error, match="did not return an access token"):
        oc.request_client_credentials(config(), session=http)


def test_transport_failures_are_wrapped() -> None:
    class Broken:
        def post(self, *args, **kwargs):
            raise OSError("connection reset")

    with pytest.raises(oc.OAuth2Error, match="could not be sent"):
        oc.request_client_credentials(config(), session=Broken())


# -- expiry -----------------------------------------------------------------


def test_expiry_is_absolute_and_applies_a_safety_margin() -> None:
    http = FakeHTTP((200, {"access_token": "t", "expires_in": 100}))
    token = oc.request_client_credentials(config(), session=http, now=1000.0)

    assert token.expires_at == 1100.0
    assert not token.is_expired(now=1000.0)
    # Inside the skew window the token counts as expired so it is refreshed
    # before it can fail mid-request.
    assert token.is_expired(now=1080.0)
    assert token.is_expired(now=1100.0)


def test_a_token_without_an_expiry_never_reports_expired() -> None:
    http = FakeHTTP((200, {"access_token": "t"}))
    token = oc.request_client_credentials(config(), session=http)
    assert not token.is_expired(now=time.time() + 10_000)


# -- loopback guard ---------------------------------------------------------


def test_authorization_code_flow_refuses_a_non_loopback_redirect() -> None:
    with pytest.raises(oc.OAuth2Error, match="loopback"):
        oc.run_authorization_code_flow(
            config(redirect_uri="https://app.example.test/callback"),
            open_browser=lambda url: pytest.fail("must not open a browser"),
        )


def test_device_flow_requires_a_device_authorization_url() -> None:
    with pytest.raises(oc.OAuth2Error, match="device authorization URL"):
        oc.request_device_code(config(device_authorization_url=""))


def test_incomplete_device_authorization_response_is_rejected() -> None:
    http = FakeHTTP((200, {"device_code": "dc"}))
    with pytest.raises(oc.OAuth2Error, match="incomplete"):
        oc.request_device_code(config(), session=http)
