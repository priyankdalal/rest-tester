"""End-to-end coverage for the non-Microsoft authentication methods.

These exercise the whole path -- profile, secret store, strategy, request
context, and the request layer's origin/transport guards -- because the
individual signing primitives are already covered by ``test_auth_signing``.
"""

from __future__ import annotations

import base64
import json
from types import SimpleNamespace

import pytest

from api_tester.auth_strategies import RequestContext, get_strategy
from api_tester.authentication import AuthenticationManager, AuthError, AuthProfile, AuthBinding
from api_tester.request_auth import AuthenticationContext


def make_manager(tmp_path) -> AuthenticationManager:
    return AuthenticationManager(
        cache_root=tmp_path / "cache",
        msal_app_factory=lambda *a, **k: pytest.fail("MSAL must not be used here"),
        persistence_factory=lambda path: pytest.fail("no persistence expected"),
        lock_factory=lambda path: SimpleNamespace(
            __enter__=lambda s: s, __exit__=lambda s, *a: False
        ),
    )


def context_for(profile: AuthProfile, manager: AuthenticationManager) -> AuthenticationContext:
    return AuthenticationContext(
        "env", {"p": profile}, {"Svc": AuthBinding(identity="p")},
        {"Svc": "https://api.example.test"}, manager,
    )


REQUEST = RequestContext(
    method="POST",
    url="https://api.example.test/items?b=2&a=1",
    headers={"Content-Type": "application/json"},
    body=b'{"name":"x"}',
    content_type="application/json",
)


# -- credential-style methods ----------------------------------------------


def test_basic_auth_builds_rfc7617_header(tmp_path) -> None:
    manager = make_manager(tmp_path)
    profile = AuthProfile(method="basic", name="Basic")
    manager.set_secrets("env", profile, {"username": "alice", "password": "s3cr3t"})

    header = manager.headers("env", profile)["Authorization"]
    scheme, encoded = header.split(" ", 1)
    assert scheme == "Basic"
    assert base64.b64decode(encoded).decode("utf-8") == "alice:s3cr3t"


def test_basic_auth_requires_a_username_but_allows_an_empty_password(tmp_path) -> None:
    """RFC 7617 permits an empty password, so only the username is mandatory."""
    manager = make_manager(tmp_path)
    profile = AuthProfile(method="basic", name="Basic")

    manager.set_secrets("env", profile, {"username": "alice", "password": ""})
    header = manager.headers("env", profile)["Authorization"]
    assert base64.b64decode(header.split(" ", 1)[1]).decode("utf-8") == "alice:"

    blank = AuthProfile(method="basic", name="Blank")
    with pytest.raises(ValueError, match="[Uu]sername"):
        blank.strategy().validate_secrets({"password": "only"})


def test_api_key_can_be_placed_in_the_query_string(tmp_path) -> None:
    manager = make_manager(tmp_path)
    profile = AuthProfile(
        method="api_key", name="Key", header_name="code",
        options={"placement": "query"},
    )
    manager.set_secret("env", profile, "key-value")

    outcome = manager.credentials("env", profile)
    assert outcome.headers == {}
    assert outcome.query == (("code", "key-value"),)
    assert get_strategy("api_key").query_names(profile) == ("code",)
    assert get_strategy("api_key").header_names(profile) == ()


def test_jwt_bearer_mints_a_signed_token(tmp_path) -> None:
    jwt = pytest.importorskip("jwt")
    manager = make_manager(tmp_path)
    profile = AuthProfile(
        method="jwt_bearer", name="JWT",
        options={
            "algorithm": "HS256", "issuer": "tester",
            "audience": "https://api.example.test", "lifetime": 300,
        },
    )
    key = "k" * 40
    manager.set_secret("env", profile, key)

    token = manager.headers("env", profile)["Authorization"].split(" ", 1)[1]
    claims = jwt.decode(token, key, algorithms=["HS256"], audience="https://api.example.test")
    assert claims["iss"] == "tester"
    assert claims["exp"] > claims["iat"]


# -- per-request signing ----------------------------------------------------


@pytest.mark.parametrize(
    "method,secrets,options",
    [
        ("aws_sigv4",
         {"access_key": "AKIDEXAMPLE", "secret_key": "s" * 40},
         {"region": "us-east-1", "service": "execute-api"}),
        ("hawk", {"key": "h" * 32}, {"key_id": "dh37fgj492je"}),
        ("oauth1", {"consumer_secret": "cs", "token_secret": "ts"},
         {"consumer_key": "ck", "token": "tk", "signature_method": "HMAC-SHA1"}),
        ("edgegrid", {"client_secret": "cs" * 10, "access_token": "akab-token"},
         {"client_token": "akab-client"}),
    ],
)
def test_signing_methods_produce_a_bound_authorization_header(
    tmp_path, method, secrets, options
) -> None:
    manager = make_manager(tmp_path)
    profile = AuthProfile(method=method, name=method, options=options)
    manager.set_secrets("env", profile, secrets)

    header = manager.credentials("env", profile, context=REQUEST).headers["Authorization"]
    assert header
    # A different request must not reuse the same signature, otherwise the
    # credential is replayable and the method is signing nothing useful.
    other = manager.credentials(
        "env", profile,
        context=RequestContext(
            method="POST", url="https://api.example.test/other", body=b"{}",
            content_type="application/json",
        ),
    ).headers["Authorization"]
    assert header != other


@pytest.mark.parametrize("method", ["aws_sigv4", "hawk", "oauth1", "edgegrid"])
def test_signing_methods_refuse_to_run_without_request_details(tmp_path, method) -> None:
    manager = make_manager(tmp_path)
    profile = AuthProfile(method=method, name=method, options={
        "region": "us-east-1", "service": "execute-api", "key_id": "k",
        "consumer_key": "ck", "token": "tk", "client_token": "ct",
    })
    manager.set_secrets("env", profile, {
        field.name: "x" * 32 for field in get_strategy(method).secret_fields
    })
    with pytest.raises(AuthError, match="without the request details"):
        manager.headers("env", profile)


def test_sigv4_signature_covers_auth_query_parameters(tmp_path) -> None:
    """A query-placed API key must be inside the signature, not beside it."""
    manager = make_manager(tmp_path)
    signer = AuthProfile(
        method="aws_sigv4", name="Sig",
        options={"region": "us-east-1", "service": "execute-api"},
    )
    manager.set_secrets("env", signer, {"access_key": "AKIDEXAMPLE", "secret_key": "s" * 40})
    key = AuthProfile(
        id="k", method="api_key", name="Key", header_name="code",
        options={"placement": "query"},
    )
    manager.set_secret("env", key, "key-value")

    context = AuthenticationContext(
        "env", {"p": signer, "k": key},
        {"Svc": AuthBinding(identity="p", api_key="k")},
        {"Svc": "https://api.example.test"}, manager,
    )
    applied = context.apply(
        "Svc", "https://api.example.test/items", {}, context=REQUEST
    )
    assert ("code", "key-value") in applied.query

    without_key = manager.credentials("env", signer, context=REQUEST).headers["Authorization"]
    assert applied.headers["Authorization"] != without_key


# -- challenge-based --------------------------------------------------------


def test_digest_defers_to_a_requests_auth_callable(tmp_path) -> None:
    manager = make_manager(tmp_path)
    profile = AuthProfile(method="digest", name="Digest")
    manager.set_secrets("env", profile, {"username": "alice", "password": "s3cr3t"})

    outcome = manager.credentials("env", profile, context=REQUEST)
    assert outcome.headers == {}
    assert outcome.request_auth is not None
    assert "s3cr3t" in outcome.secrets


def test_digest_still_honours_the_origin_and_transport_guards(tmp_path) -> None:
    """Digest signs inside requests, so the guards must run before it is handed over."""
    manager = make_manager(tmp_path)
    profile = AuthProfile(id="p", method="digest", name="Digest")
    manager.set_secrets("env", profile, {"username": "alice", "password": "s3cr3t"})
    context = context_for(profile, manager)

    with pytest.raises(AuthError, match="origin differs"):
        context.apply("Svc", "https://evil.example/items", {}, context=REQUEST)

    plain = AuthenticationContext(
        "env", {"p": profile}, {"Svc": AuthBinding(identity="p")},
        {"Svc": "http://api.example.test"}, manager,
    )
    with pytest.raises(AuthError, match="require HTTPS"):
        plain.apply("Svc", "http://api.example.test/items", {}, context=REQUEST)


def test_only_one_challenge_based_method_may_apply(tmp_path) -> None:
    manager = make_manager(tmp_path)
    first = AuthProfile(id="p", method="digest", name="D1")
    second = AuthProfile(id="k", method="digest", name="D2")
    for profile in (first, second):
        manager.set_secrets("env", profile, {"username": "a", "password": "b"})

    context = AuthenticationContext(
        "env", {"p": first, "k": second},
        {"Svc": AuthBinding(identity="p", api_key="k")},
        {"Svc": "https://api.example.test"}, manager,
    )
    # The api_key slot only accepts api-key-like methods, which is the first
    # guard a second Digest profile runs into.
    with pytest.raises(AuthError, match="identity/API-key binding"):
        context.apply("Svc", "https://api.example.test/items", {}, context=REQUEST)


# -- no_auth ----------------------------------------------------------------


def test_no_auth_contributes_nothing(tmp_path) -> None:
    manager = make_manager(tmp_path)
    profile = AuthProfile(method="no_auth", name="None")
    outcome = manager.credentials("env", profile)
    assert outcome.headers == {} and outcome.query == ()
    assert get_strategy("no_auth").header_names(profile) == ()


# -- redaction --------------------------------------------------------------


def test_basic_redaction_covers_the_plaintext_password_not_just_the_blob(tmp_path) -> None:
    """A server that echoes a decoded credential must not leak it into output."""
    manager = make_manager(tmp_path)
    profile = AuthProfile(id="p", method="basic", name="Basic")
    manager.set_secrets("env", profile, {"username": "alice", "password": "PW-PLAINTEXT"})
    context = context_for(profile, manager)
    applied = context.apply("Svc", "https://api.example.test/items", {}, context=REQUEST)

    echoed = "server said: PW-PLAINTEXT for " + applied.headers["Authorization"]
    cleaned = applied.redact(echoed)
    assert "PW-PLAINTEXT" not in cleaned
    assert applied.headers["Authorization"] not in cleaned


@pytest.mark.parametrize(
    "method,secrets,options",
    [
        ("basic", {"username": "alice", "password": "s3cr3t-value"}, {}),
        ("manual_bearer", {"token": "tok-abcdef-123456"}, {}),
        ("api_key", {"key": "key-abcdef-123456"}, {"header_name": "X-Key"}),
    ],
)
def test_applied_credentials_are_redacted_from_output(
    tmp_path, method, secrets, options
) -> None:
    manager = make_manager(tmp_path)
    profile = AuthProfile(id="p", method=method, name=method, options=options,
                          header_name=options.get("header_name", "x-api-key"))
    manager.set_secrets("env", profile, secrets)
    binding = AuthBinding(api_key="p") if method == "api_key" else AuthBinding(identity="p")
    context = AuthenticationContext(
        "env", {"p": profile}, {"Svc": binding},
        {"Svc": "https://api.example.test"}, manager,
    )
    applied = context.apply("Svc", "https://api.example.test/items", {}, context=REQUEST)

    # Whatever actually goes on the wire must be redactable, including the
    # derived forms (a Basic blob, a bare token) rather than only the inputs.
    reflected = json.dumps(applied.headers)
    cleaned = applied.redact(reflected)
    for value in applied.headers.values():
        assert value in reflected and value not in cleaned
    for value in secrets.values():
        if value in reflected:
            assert value not in cleaned
