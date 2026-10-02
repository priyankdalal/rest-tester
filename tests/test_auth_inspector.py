"""The auth inspector must answer "what is being sent, and what happened?".

These cover the two halves separately: the event log must never carry a
credential value, and the inspector must report the value that would
actually go on the wire.
"""

from __future__ import annotations

import base64
import json
import time

import pytest

from tests.test_authentication_ui import (  # noqa: F401 - fixtures
    FakeAuthenticationManager,
    make_profile,
    qt_app,
)

from api_tester import auth_inspect, auth_log
from api_tester.authentication import AuthenticationManager, AuthError, AuthProfile


@pytest.fixture(name="app")
def _app(qt_app):  # noqa: F811
    return qt_app


def _jwt(**claims) -> str:
    def segment(data) -> str:
        return base64.urlsafe_b64encode(json.dumps(data).encode()).decode().rstrip("=")

    payload = {"sub": "user-1", "exp": time.time() + 3600, **claims}
    return ".".join([segment({"alg": "RS256", "typ": "JWT"}), segment(payload), "sig"])


# --------------------------------------------------------------- the log


def test_the_log_keeps_only_the_most_recent_entries() -> None:
    log = auth_log.AuthActivityLog(capacity=3)
    for index in range(6):
        log.add(auth_log.APPLIED, f"p{index}", "api_key")
    assert len(log) == 3
    assert [entry.profile for entry in log.entries()] == ["p5", "p4", "p3"]


def test_entries_are_newest_first_by_default() -> None:
    log = auth_log.AuthActivityLog()
    log.add(auth_log.APPLIED, "first", "api_key")
    log.add(auth_log.APPLIED, "second", "api_key")
    assert [entry.profile for entry in log.entries()][0] == "second"
    assert [entry.profile for entry in log.entries(newest_first=False)][0] == "first"


def test_entries_can_be_filtered_to_one_profile() -> None:
    log = auth_log.AuthActivityLog()
    log.add(auth_log.APPLIED, "alpha", "api_key")
    log.add(auth_log.APPLIED, "beta", "api_key")
    assert [entry.profile for entry in log.entries(profile="beta")] == ["beta"]


def test_the_log_records_names_but_never_values() -> None:
    """The log is copied into bug reports, so this has to be structural."""
    log = auth_log.AuthActivityLog()
    entry = log.add(
        auth_log.APPLIED, "Key", "api_key", header_names=("x-api-key",)
    )
    assert entry.header_names == ("x-api-key",)
    assert not hasattr(entry, "values")
    assert "secret" not in entry.summary().lower()


def test_a_failed_entry_is_marked_not_ok() -> None:
    log = auth_log.AuthActivityLog()
    entry = log.add(auth_log.FAILED, "Key", "api_key", detail="boom", ok=False)
    assert not entry.ok
    assert "boom" in entry.summary()


def test_applying_a_credential_is_recorded_with_its_header_name() -> None:
    log = auth_log.AuthActivityLog()
    manager = AuthenticationManager(activity_log=log)
    profile = AuthProfile(id="k", name="Key", method="api_key", header_name="x-api-key")
    manager.set_secrets("env", profile, {"key": "value-1"})

    manager.credentials("env", profile)

    entry = log.entries()[0]
    assert entry.event == auth_log.APPLIED
    assert entry.header_names == ("x-api-key",)
    assert "value-1" not in entry.summary()


def test_a_missing_secret_is_recorded_as_a_failure() -> None:
    log = auth_log.AuthActivityLog()
    manager = AuthenticationManager(activity_log=log)
    profile = AuthProfile(id="k", name="Key", method="api_key", header_name="x-api-key")

    with pytest.raises(AuthError):
        manager.credentials("env", profile)

    entry = log.entries()[0]
    assert entry.event == auth_log.FAILED
    assert not entry.ok


def test_clearing_a_session_is_recorded(tmp_path) -> None:
    log = auth_log.AuthActivityLog()
    manager = AuthenticationManager(cache_root=tmp_path, activity_log=log)
    profile = AuthProfile(id="k", name="Key", method="api_key", header_name="x-api-key")
    manager.set_secrets("env", profile, {"key": "value"})
    log.clear()

    manager.clear_session("env", profile)

    assert log.entries()[0].event == auth_log.SIGNED_OUT


# ---------------------------------------------------------- jwt decoding


def test_a_jwt_is_decoded_into_claims() -> None:
    claims = auth_inspect.decode_jwt(_jwt(scp="a.read a.write", roles=["Admin"]))
    assert claims is not None
    assert claims.subject == "user-1"
    assert claims.scopes == ("a.read", "a.write")
    assert claims.roles == ("Admin",)
    assert 3500 < claims.expires_in() <= 3600


def test_a_bearer_prefix_is_tolerated() -> None:
    assert auth_inspect.decode_jwt("Bearer " + _jwt()) is not None


def test_a_scope_list_is_accepted_as_well_as_a_string() -> None:
    assert auth_inspect.decode_jwt(_jwt(scope=["x", "y"])).scopes == ("x", "y")


@pytest.mark.parametrize(
    "value", ["", "plain-key", "a.b", "a.b.c.d", "not!base64.nope.sig"]
)
def test_a_non_jwt_value_decodes_to_nothing_rather_than_raising(value) -> None:
    assert auth_inspect.decode_jwt(value) is None


def test_an_expired_token_reports_negative_time_remaining() -> None:
    claims = auth_inspect.decode_jwt(_jwt(exp=time.time() - 60))
    assert claims.expires_in() < 0


# ------------------------------------------------------------- inspector


def test_inspecting_reports_the_value_that_would_be_sent() -> None:
    manager = AuthenticationManager()
    profile = AuthProfile(id="k", name="Key", method="api_key", header_name="x-api-key")
    manager.set_secrets("env", profile, {"key": "the-real-key"})

    snapshot = auth_inspect.inspect(manager, "env", profile)

    assert snapshot.ok
    assert [(v.placement, v.name, v.value) for v in snapshot.values] == [
        ("header", "x-api-key", "the-real-key")
    ]


def test_a_query_placed_key_is_reported_as_a_query_value() -> None:
    manager = AuthenticationManager()
    profile = AuthProfile(
        id="k", name="Key", method="api_key", header_name="code",
        options={"placement": "query"},
    )
    manager.set_secrets("env", profile, {"key": "abc"})

    snapshot = auth_inspect.inspect(manager, "env", profile)

    assert [(v.placement, v.name) for v in snapshot.values] == [("query", "code")]


def test_inspecting_a_bearer_token_surfaces_its_claims() -> None:
    manager = AuthenticationManager()
    profile = AuthProfile(id="b", name="Token", method="manual_bearer")
    manager.set_secrets("env", profile, {"token": _jwt(roles=["Tester"])})

    snapshot = auth_inspect.inspect(manager, "env", profile)

    assert snapshot.claims is not None
    assert snapshot.claims.roles == ("Tester",)


def test_an_unconfigured_profile_reports_the_error_instead_of_raising() -> None:
    manager = AuthenticationManager()
    profile = AuthProfile(id="k", name="Key", method="api_key", header_name="x-api-key")

    snapshot = auth_inspect.inspect(manager, "env", profile)

    assert not snapshot.ok
    assert snapshot.error
    assert snapshot.values == ()


def test_inspecting_never_triggers_an_interactive_sign_in(monkeypatch) -> None:
    """A browser prompt from a read-only inspector would be a trap."""
    manager = AuthenticationManager()
    profile = AuthProfile(
        id="e", name="Entra", method="entra_pkce",
        authority="https://login.microsoftonline.com/organizations",
        client_id="00000000-0000-0000-0000-000000000001",
        scopes=["api://example/access"],
    )

    def fail(*args, **kwargs):
        raise AssertionError("inspect must not sign in")

    monkeypatch.setattr(manager, "sign_in", fail)
    snapshot = auth_inspect.inspect(manager, "env", profile)

    assert not snapshot.ok


def test_a_signing_method_is_flagged_as_computed_per_request() -> None:
    manager = AuthenticationManager()
    profile = AuthProfile(
        id="s", name="AWS", method="aws_sigv4",
        options={"region": "us-east-1", "service": "execute-api"},
    )
    manager.set_secrets(
        "env", profile, {"access_key": "AKIDEXAMPLE", "secret_key": "s" * 40}
    )

    snapshot = auth_inspect.inspect(manager, "env", profile)

    assert snapshot.per_request
    assert snapshot.ok


def test_a_challenge_method_explains_why_there_is_no_value_yet() -> None:
    manager = AuthenticationManager()
    profile = AuthProfile(id="d", name="Digest", method="digest")
    manager.set_secrets("env", profile, {"username": "u", "password": "p"})

    snapshot = auth_inspect.inspect(manager, "env", profile)

    assert snapshot.per_request
    assert snapshot.values == ()
    assert "challenge" in snapshot.error.lower()


def test_the_snapshot_lists_configuration_without_secrets() -> None:
    manager = AuthenticationManager()
    profile = AuthProfile(
        id="s", name="AWS", method="aws_sigv4",
        options={"region": "eu-west-1", "service": "s3"},
    )
    manager.set_secrets(
        "env", profile, {"access_key": "AKIDEXAMPLE", "secret_key": "s" * 40}
    )

    snapshot = auth_inspect.inspect(manager, "env", profile)

    rendered = " ".join(f"{label}{value}" for label, value in snapshot.config)
    assert "eu-west-1" in rendered
    assert "AKIDEXAMPLE" not in rendered
    assert "s" * 40 not in rendered


# ------------------------------------------------- flow tracing / http legs


class _Response:
    def __init__(self, payload, status=200):
        self._payload = payload
        self.status_code = status

    def json(self):
        return self._payload


class _Session:
    def __init__(self, payload=None, status=200, error=None):
        self._payload = payload or {
            "access_token": "tok-abc", "expires_in": 3600, "token_type": "Bearer"
        }
        self._status = status
        self._error = error
        self.calls = []

    def post(self, url, data=None, headers=None, timeout=None):
        self.calls.append((url, data))
        if self._error:
            raise self._error
        return _Response(self._payload, self._status)


def _config(**kwargs):
    from api_tester.oauth2_client import OAuth2Config

    kwargs.setdefault("token_url", "https://id.example.com/token")
    return OAuth2Config(client_id="cid", **kwargs)


def test_a_token_call_is_logged_with_its_url_status_and_duration() -> None:
    from api_tester import oauth2_client

    log = auth_log.AuthActivityLog()
    with auth_log.flow("My OAuth", "oauth2", environment="dev", log=log):
        oauth2_client.request_client_credentials(_config(), session=_Session())

    entry = log.entries()[0]
    assert entry.event == auth_log.HTTP
    assert entry.http_method == "POST"
    assert entry.url.startswith("https://id.example.com/token")
    assert entry.status == 200
    assert entry.profile == "My OAuth"
    assert entry.environment == "dev"
    assert "grant_type" in entry.sent_fields


def test_a_token_url_query_string_is_never_stored() -> None:
    """Token endpoints accept client_secret and code as query parameters."""
    from api_tester import oauth2_client

    log = auth_log.AuthActivityLog()
    config = _config(token_url="https://id.example.com/token?client_secret=leak")
    with auth_log.flow("P", "oauth2", log=log):
        oauth2_client.request_client_credentials(config, session=_Session())

    assert "leak" not in log.entries()[0].url
    assert "(+params)" in log.entries()[0].url


def test_only_field_names_are_logged_never_their_values() -> None:
    from api_tester import oauth2_client

    log = auth_log.AuthActivityLog()
    config = _config(client_secret="shh-secret", client_auth="body")
    with auth_log.flow("P", "oauth2", log=log):
        oauth2_client.request_client_credentials(config, session=_Session())

    entry = log.entries()[0]
    assert "client_secret" in entry.sent_fields
    assert "shh-secret" not in entry.summary()
    assert "tok-abc" not in entry.summary()


def test_a_provider_error_is_logged_as_a_failed_leg() -> None:
    from api_tester import oauth2_client

    log = auth_log.AuthActivityLog()
    session = _Session({"error": "invalid_client"}, status=401)
    with auth_log.flow("P", "oauth2", log=log):
        with pytest.raises(oauth2_client.OAuth2Error):
            oauth2_client.request_client_credentials(_config(), session=session)

    entry = log.entries()[0]
    assert entry.status == 401
    assert not entry.ok


def test_a_transport_failure_is_logged_rather_than_lost() -> None:
    from api_tester import oauth2_client

    log = auth_log.AuthActivityLog()
    session = _Session(error=OSError("connection refused"))
    with auth_log.flow("P", "oauth2", log=log):
        with pytest.raises(oauth2_client.OAuth2Error):
            oauth2_client.request_client_credentials(_config(), session=session)

    entry = log.entries()[0]
    assert not entry.ok
    assert "connection refused" in entry.detail


def test_building_an_authorization_url_records_the_step() -> None:
    from api_tester import oauth2_client

    log = auth_log.AuthActivityLog()
    config = _config(authorization_url="https://id.example.com/authorize")
    with auth_log.flow("P", "oauth2", log=log):
        oauth2_client.build_authorization_url(
            config, redirect_uri="http://localhost:1", state="s", code_challenge="c"
        )

    entry = log.entries()[0]
    assert entry.event == auth_log.STEP
    assert "PKCE" in entry.detail
    assert "code_challenge" in entry.sent_fields


def test_tracing_outside_a_flow_is_a_no_op() -> None:
    """The pure helpers must stay usable without a logging context."""
    assert auth_log.trace(auth_log.STEP, detail="orphan") is None


def test_a_flow_does_not_leak_into_the_shared_log() -> None:
    log = auth_log.AuthActivityLog()
    before = len(auth_log.activity_log)
    with auth_log.flow("P", "oauth2", log=log):
        auth_log.trace(auth_log.STEP, detail="scoped")
    assert len(log) == 1
    assert len(auth_log.activity_log) == before


def test_nested_flows_restore_the_outer_context() -> None:
    outer, inner = auth_log.AuthActivityLog(), auth_log.AuthActivityLog()
    with auth_log.flow("Outer", "oauth2", log=outer):
        with auth_log.flow("Inner", "api_key", log=inner):
            auth_log.trace(auth_log.STEP, detail="in")
        auth_log.trace(auth_log.STEP, detail="out")

    assert [e.profile for e in inner.entries()] == ["Inner"]
    assert [e.profile for e in outer.entries()] == ["Outer"]


def test_credentials_publishes_a_flow_context() -> None:
    """Without this, token legs would be recorded with no profile."""
    log = auth_log.AuthActivityLog()
    manager = AuthenticationManager(activity_log=log)
    profile = AuthProfile(id="k", name="Key", method="api_key", header_name="x-api-key")
    manager.set_secrets("env", profile, {"key": "v"})

    captured = []
    original = auth_log.trace

    def spy(event, **fields):
        captured.append(auth_log._current.get())
        return original(event, **fields)

    auth_log.trace = spy
    try:
        auth_log.trace(auth_log.STEP, detail="outside")
        manager.credentials("env", profile)
    finally:
        auth_log.trace = original

    assert captured[0] is None, "no context before the call"
    assert auth_log._current.get() is None, "context torn down after the call"


def test_a_token_leg_is_attributed_to_the_profile_that_caused_it() -> None:
    from api_tester import oauth2_client

    log = auth_log.AuthActivityLog()
    manager = AuthenticationManager(activity_log=log)
    profile = AuthProfile(
        id="o", name="Partner OAuth", method="oauth2", client_id="cid",
        options={
            "grant_type": "client_credentials",
            "token_url": "https://id.example.com/token",
        },
    )
    manager.set_secrets("env", profile, {"client_secret": "cs"})
    session = _Session()
    original = oauth2_client.request_client_credentials
    oauth2_client.request_client_credentials = (
        lambda config, **kwargs: original(config, session=session)
    )
    try:
        manager.credentials("env", profile)
    finally:
        oauth2_client.request_client_credentials = original

    http = [e for e in log.entries() if e.event == auth_log.HTTP]
    assert http, "the token call was not logged"
    assert http[0].profile == "Partner OAuth"
    assert http[0].environment == "env"
    applied = [e for e in log.entries() if e.event == auth_log.APPLIED]
    assert applied and applied[0].header_names == ("Authorization",)
