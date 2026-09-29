from dataclasses import replace
from types import SimpleNamespace

import pytest
import requests

from api_tester.authentication import AuthBinding, AuthError, AuthProfile, InteractionRequired
from api_tester.catalog import Endpoint, Parameter
from api_tester.client import execute_endpoint
from api_tester.request_auth import AuthenticationContext
from api_tester.runner import run_suite
from api_tester.saved_requests import SavedRequest
from api_tester.suite import TestCase as Case, TestSuite as Suite
from api_tester.auth_strategies import AuthOutcome
from api_tester.suite import Capture


class Manager:
    def __init__(self):
        self.calls = []
        self.error = None

    def headers(self, environment_id, profile, force_refresh=False):
        return self.credentials(
            environment_id, profile, force_refresh=force_refresh
        ).headers

    def credentials(self, environment_id, profile, *, context=None, force_refresh=False):
        self.calls.append((environment_id, profile.id, force_refresh))
        if self.error:
            raise self.error
        if profile.method == "api_key":
            value = "example-key-value"
            if profile.options.get("placement") == "query":
                return AuthOutcome(query=((profile.header_name, value),), secrets=(value,))
            return AuthOutcome(headers={profile.header_name: value}, secrets=(value,))
        token = "Bearer example-renewed-token" if force_refresh else "Bearer example-token"
        return AuthOutcome(
            headers={"Authorization": token},
            secrets=(token, token.split(" ", 1)[1]),
        )


@pytest.fixture
def context():
    identity = AuthProfile(
        id="identity", name="Test user", method="entra_pkce",
        authority="https://login.microsoftonline.com/organizations",
        client_id="00000000-0000-0000-0000-000000000001", scopes=["api://example/access"],
    )
    key = AuthProfile(id="key", method="api_key", header_name="X-Subscription")
    return AuthenticationContext(
        "environment", {"identity": identity, "key": key},
        {"Test": AuthBinding(identity="identity", api_key="key")},
        {"Test": "https://example.test"}, Manager(),
    )


@pytest.fixture
def endpoint():
    return Endpoint(id="test.get", service="Test", controller="Test", action="Get",
                    method="GET", path="/items")


@pytest.fixture
def transport(monkeypatch):
    calls = []
    responses = []

    def request(method, url, **kwargs):
        calls.append((method, url, kwargs))
        status, body, headers = responses.pop(0) if responses else (200, b"{}", {})
        return SimpleNamespace(
            status_code=status, content=body, headers={"content-type": "application/json", **headers},
            url=url, request=SimpleNamespace(headers=kwargs["headers"], body=None), reason="Result",
        )

    monkeypatch.setattr("api_tester.client.requests.request", request)
    monkeypatch.setattr("api_tester.client.measure_connection", lambda *args: {})
    return calls, responses


def execute(endpoint, context, **kwargs):
    return execute_endpoint(endpoint, "https://example.test", "", "", {}, None, "200",
                            auth_context=context, **kwargs)


def test_preview_never_acquires_credentials(context):
    applied = context.apply("Test", "https://example.test/items", {}, preview=True)
    assert applied.headers == {"Authorization": "******", "X-Subscription": "******"}
    assert not context.manager.calls


def test_preview_masks_query_placed_keys_so_the_url_matches_what_is_sent(context):
    context.profiles["key"].options["placement"] = "query"
    applied = context.apply("Test", "https://example.test/items", {}, preview=True)
    assert applied.query == (("X-Subscription", "******"),)
    assert "X-Subscription" not in applied.headers
    assert not context.manager.calls


def test_a_required_query_parameter_supplied_by_auth_is_not_reported_missing(
    context, endpoint, transport
):
    context.profiles["key"].options["placement"] = "query"
    endpoint = replace(endpoint, parameters=(Parameter("X-Subscription", "query", "string", True),))
    calls, _ = transport
    execute(endpoint, context)
    assert calls[0][2]["params"]["X-Subscription"] == "example-key-value"


def test_snapshot_keeps_original_profile_and_binding(context):
    profiles = context.profiles
    bindings = context.bindings
    snapshot = AuthenticationContext("original", profiles, bindings, context.base_urls, context.manager)
    profiles["identity"].client_id = "changed"
    bindings["Test"].identity = "missing"
    assert snapshot.profiles["identity"].client_id != "changed"
    assert snapshot.bindings["Test"].identity == "identity"


@pytest.mark.parametrize("url", ["https://different.test/items", "http://example.test/items"])
def test_managed_credentials_do_not_leave_configured_secure_origin(context, url):
    with pytest.raises(AuthError):
        context.apply("Test", url, {})
    assert not context.manager.calls


def test_conflicting_manual_headers_are_not_silently_overwritten(context):
    with pytest.raises(AuthError, match="conflicts"):
        context.apply("Test", "https://example.test", {"authorization": "manual-value"})
    assert not context.manager.calls


def test_manual_override_does_not_acquire_or_replace(context):
    headers = {"Authorization": "manual-value"}
    assert context.apply("Test", "https://example.test", headers, "manual").headers == headers
    assert not context.manager.calls


def test_no_auth_removes_known_and_custom_credentials_even_required_headers(context, endpoint, transport):
    endpoint = replace(endpoint, parameters=(Parameter("Authorization", "header", "string", True),))
    calls, _ = transport
    execute_endpoint(
        endpoint, "https://example.test", "manual-token", "manual-key", {}, None, "200",
        custom_headers={"Cookie": "session", "X-Subscription": "key", "X-Trace": "trace"},
        auth_context=context, auth_mode="none",
    )
    assert calls[0][2]["headers"] == {"Accept": "application/json", "X-Trace": "trace"}
    assert not context.manager.calls


def test_no_auth_also_works_without_environment_context(endpoint, transport):
    calls, _ = transport
    execute_endpoint(endpoint, "https://example.test", "manual-token", "manual-key",
                     {}, None, "200", auth_mode="none")
    assert "Authorization" not in calls[0][2]["headers"]
    assert "x-api-key" not in calls[0][2]["headers"]


def test_managed_header_satisfies_required_catalog_parameter(context, endpoint, transport):
    endpoint = replace(endpoint, parameters=(Parameter("Authorization", "header", "string", True),))
    assert execute(endpoint, context).passed
    assert transport[0][0][2]["headers"]["Authorization"].endswith("example-token")


def test_managed_execution_redacts_reflected_secrets_and_disables_redirects(context, endpoint, transport):
    calls, responses = transport
    responses.append((200, b'{"token":"example-token","key":"example-key-value"}',
                      {"X-Echo": "example-token", "X-Subscription": "example-key-value",
                       "Set-Cookie": "response-session"}))
    result = execute(endpoint, context)
    assert calls[0][2]["allow_redirects"] is False
    assert calls[0][2]["headers"]["Authorization"].endswith("example-token")
    assert result.request_headers["X-Subscription"] == "******"
    assert result.response_headers["Set-Cookie"] == "******"
    assert "example-token" not in repr(result)
    assert "example-key-value" not in repr(result)
    assert result.timings["auth_retry_count"] == 0
    assert result.timings["auth_ms"] >= 0


@pytest.mark.parametrize(
    ("method", "status", "challenge", "expected", "count"),
    [
        ("GET", 401, 'Bearer error="invalid_token"', "200", 2),
        ("HEAD", 401, 'Bearer error="invalid_token"', "200", 2),
        ("GET", 401, 'Bearer error="invalid_token"', "401", 1),
        ("GET", 401, 'Bearer error="invalid_token"', "200-499", 1),
        ("GET", 401, "Bearer", "200", 1),
        ("GET", 401, 'Bearer error="insufficient_scope"', "200", 1),
        ("GET", 401, 'Bearer error="invalid_token_other"', "200", 1),
        ("GET", 401, 'Basic realm="Bearer error=invalid_token"', "200", 1),
        ("GET", 403, 'Bearer error="invalid_token"', "200", 1),
        ("POST", 401, 'Bearer error="invalid_token"', "200", 1),
        ("PATCH", 401, 'Bearer error="invalid_token"', "200", 1),
        ("DELETE", 401, 'Bearer error="invalid_token"', "200", 1),
    ],
)
def test_only_safe_invalid_token_requests_retry_once(
    context, endpoint, transport, method, status, challenge, expected, count
):
    calls, responses = transport
    responses.extend([(status, b"{}", {"WWW-Authenticate": challenge})] * 2)
    result = execute_endpoint(replace(endpoint, method=method), "https://example.test",
                              "", "", {}, None, expected, auth_context=context)
    assert len(calls) == count
    assert result.timings["auth_retry_count"] == count - 1
    assert len([call for call in context.manager.calls if call[2]]) == (2 if count == 2 else 0)


def test_transport_error_redacts_managed_credentials(context, endpoint, monkeypatch):
    monkeypatch.setattr("api_tester.client.measure_connection", lambda *args: {})

    def fail(*args, **kwargs):
        raise requests.exceptions.InvalidHeader("Invalid header example-token")

    monkeypatch.setattr("api_tester.client.requests.request", fail)
    with pytest.raises(RuntimeError) as error:
        execute(endpoint, context)
    assert "example-token" not in str(error.value)


def test_retry_redacts_both_original_and_replacement_credentials(context, endpoint, transport):
    transport[1].extend([
        (401, b"{}", {"WWW-Authenticate": 'Bearer error="invalid_token"'}),
        (200, b'{"old":"example-token","new":"example-renewed-token"}', {}),
    ])
    result = execute(endpoint, context)
    assert "example-token" not in repr(result)
    assert "example-renewed-token" not in repr(result)


def test_suite_captures_cannot_persist_reflected_managed_tokens(context, endpoint, transport):
    transport[1].append((200, b'{"token":"example-token"}', {}))
    case = Case(endpoint_id=endpoint.id, captures=[Capture(name="reflected", path="token")])
    result = run_suite(Suite(cases=[case]), {endpoint.id: endpoint},
                       context.base_urls, "", "", auth_context=context)
    assert result.results[0].captured == {"reflected": "******"}


def test_missing_profile_fails_without_sending(context, endpoint, transport):
    with pytest.raises(AuthError, match="missing"):
        execute(endpoint, context, auth_mode="unknown")
    assert not transport[0]


def test_no_auth_blocks_ambient_netrc_credentials(endpoint, transport, monkeypatch):
    execute_endpoint(endpoint, "https://example.test", "", "", {}, None, "200", auth_mode="none")
    options = transport[0][0][2]
    assert options["allow_redirects"] is False

    def unexpected(*args):
        pytest.fail("No-auth request attempted to read ambient netrc credentials")

    monkeypatch.setattr("requests.sessions.get_netrc_auth", unexpected)
    with requests.Session() as session:
        prepared = session.prepare_request(requests.Request(
            "GET", "https://example.test", headers=options["headers"], auth=options["auth"],
        ))
    assert "Authorization" not in prepared.headers


def test_suite_stops_on_interaction_required_without_anonymous_fallback(context, endpoint, transport):
    context.manager.error = InteractionRequired("Sign in from Authentication settings.")
    suite = Suite(name="Auth", stop_on_failure=False,
                  cases=[Case(endpoint_id=endpoint.id), Case(endpoint_id=endpoint.id)])
    result = run_suite(suite, {endpoint.id: endpoint}, context.base_urls, "", "", auth_context=context)
    assert result.results[0].authentication_failed
    assert "Sign in" in result.results[0].error
    assert result.results[1].skipped
    assert not transport[0]


def test_negative_case_does_not_need_a_signed_in_session(context, endpoint, transport):
    context.manager.error = InteractionRequired("Sign in")
    transport[1].append((401, b"{}", {}))
    suite = Suite(name="Negative", cases=[
        Case(endpoint_id=endpoint.id, authentication="none", expected_status="401"),
    ])
    result = run_suite(suite, {endpoint.id: endpoint}, context.base_urls, "", "", auth_context=context)
    assert result.passed == 1
    assert not context.manager.calls


@pytest.mark.parametrize("model", [Case, SavedRequest])
def test_authentication_selection_roundtrips_without_breaking_old_files(model):
    source = {"endpoint_id": "test", "name": "A request"}
    assert model.from_dict(source).authentication == "inherit"
    source["authentication"] = "identity"
    assert model.from_dict(model.from_dict(source).to_dict()).authentication == "identity"
