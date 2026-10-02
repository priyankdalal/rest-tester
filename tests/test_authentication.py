"""Tests for the authentication engine.

Everything here uses fakes injected through ``AuthenticationManager``'s hooks
(``msal_app_factory``, ``persistence_factory``, ``cache_serializer_factory``,
``persisted_cache_factory``); the real ``msal``/``msal_extensions`` packages
are never imported and no test performs a live network/identity call.
"""

from __future__ import annotations

import collections
import json
import threading
import time

import pytest

from api_tester.authentication import (
    AuthBinding,
    AuthenticationManager,
    AuthError,
    AuthProfile,
    InteractionRequired,
    get_auth_manager,
)


class FakePublicClientApp:
    """Stands in for ``msal.PublicClientApplication``."""

    def __init__(self, client_id, authority=None, token_cache=None):
        self.client_id = client_id
        self.authority = authority
        self.token_cache = token_cache
        self.accounts: list = []
        self.silent_results = collections.deque()
        self.default_silent_result = None
        self.interactive_result = {"access_token": "interactive-token", "expires_in": 3600}
        self.interactive_exception = None
        self.silent_calls: list = []
        self.silent_with_error_calls = 0
        self.interactive_calls: list = []
        self.removed_accounts: list = []

    def get_accounts(self):
        return list(self.accounts)

    def acquire_token_silent(self, scopes, account=None, force_refresh=False):
        self.silent_calls.append(
            {"scopes": list(scopes), "account": account, "force_refresh": force_refresh}
        )
        if self.silent_results:
            return self.silent_results.popleft()
        return self.default_silent_result

    def acquire_token_silent_with_error(self, scopes, account=None, force_refresh=False):
        self.silent_with_error_calls += 1
        return self.acquire_token_silent(scopes, account=account, force_refresh=force_refresh)

    def acquire_token_interactive(self, scopes, timeout=None, port=None):
        self.interactive_calls.append(
            {"scopes": list(scopes), "timeout": timeout, "port": port}
        )
        if self.interactive_exception is not None:
            raise self.interactive_exception
        return self.interactive_result

    def remove_account(self, account):
        self.removed_accounts.append(account)
        if account in self.accounts:
            self.accounts.remove(account)


class FakeConfidentialClientApp:
    """Stands in for ``msal.ConfidentialClientApplication``."""

    def __init__(self, client_id, client_credential=None, authority=None, token_cache=None):
        self.client_id = client_id
        self.client_credential = client_credential
        self.authority = authority
        self.token_cache = token_cache
        self.for_client_result = {"access_token": "app-token", "expires_in": 3600}
        self.calls: list = []
        self.remove_tokens_calls = 0

    def acquire_token_for_client(self, scopes):
        self.calls.append({"scopes": list(scopes)})
        return self.for_client_result

    def remove_tokens_for_client(self):
        self.remove_tokens_calls += 1


class FakeCache:
    def __init__(self):
        self.has_state_changed = False
        self.serialized = None

    def serialize(self):
        return "serialized-cache-state"


def make_manager(tmp_path, *, public_app=None, confidential_app=None, cache=None, **kwargs):
    public_app = public_app or FakePublicClientApp("client")
    confidential_app = confidential_app or FakeConfidentialClientApp("client")
    cache = cache or FakeCache()

    def factory(kind, profile, built_cache, secret):
        if kind == "confidential":
            confidential_app.client_credential = secret
            return confidential_app
        return public_app

    defaults = dict(
        cache_root=tmp_path / "auth-cache",
        msal_app_factory=factory,
        cache_serializer_factory=lambda: cache,
    )
    defaults.update(kwargs)
    return AuthenticationManager(**defaults)


def oauth_profile(**overrides):
    values = dict(
        method="entra_pkce",
        authority="https://login.microsoftonline.com/contoso-tenant",
        client_id="11111111-1111-1111-1111-111111111111",
        scopes=["api://app/.default"],
    )
    values.update(overrides)
    return AuthProfile(**values)


# -- AuthProfile / AuthBinding validation and (de)serialization -------------


def test_auth_profile_validate_rejects_unknown_method() -> None:
    profile = AuthProfile(method="device_code")
    with pytest.raises(ValueError, match="Unsupported authentication method"):
        profile.validate()


def test_auth_profile_validate_requires_https_authority() -> None:
    profile = oauth_profile(authority="http://login.microsoftonline.com/contoso-tenant")
    with pytest.raises(ValueError, match="HTTPS"):
        profile.validate()


def test_auth_profile_validate_rejects_query_or_userinfo_in_authority() -> None:
    profile = oauth_profile(authority="https://login.microsoftonline.com/contoso-tenant?x=1")
    with pytest.raises(ValueError, match="query string"):
        profile.validate()
    profile = oauth_profile(authority="https://user:pw@login.microsoftonline.com/contoso-tenant")
    with pytest.raises(ValueError, match="credentials"):
        profile.validate()


def test_auth_profile_validate_requires_b2c_policy_segment() -> None:
    profile = oauth_profile(
        method="b2c_pkce", authority="https://contoso.b2clogin.com/contoso-tenant"
    )
    with pytest.raises(ValueError, match="tenant and policy"):
        profile.validate()
    profile.authority = "https://contoso.b2clogin.com/contoso-tenant/B2C_1_signupsignin"
    profile.validate()  # does not raise


def test_auth_profile_validate_requires_scopes_and_client_id_for_oauth() -> None:
    profile = oauth_profile(client_id="")
    with pytest.raises(ValueError, match="Client ID"):
        profile.validate()
    profile = oauth_profile(scopes=[])
    with pytest.raises(ValueError, match="scope"):
        profile.validate()
    profile = oauth_profile(scopes=["has space"])
    with pytest.raises(ValueError, match="whitespace"):
        profile.validate()


def test_auth_profile_validate_requires_valid_api_key_header_name() -> None:
    profile = AuthProfile(method="api_key", header_name="Bad Header Name")
    with pytest.raises(ValueError, match="header"):
        profile.validate()


def test_auth_profile_to_dict_from_dict_round_trip_has_no_secret_fields() -> None:
    profile = oauth_profile(name="My Login", persistent=True, login_timeout=90)
    document = profile.to_dict()
    assert set(document) == {
        "id",
        "name",
        "method",
        "authority",
        "client_id",
        "scopes",
        "header_name",
        "persistent",
        "login_timeout",
        "redirect_uri",
        "options",
    }
    restored = AuthProfile.from_dict(document)
    assert restored == profile


def test_auth_profile_options_never_persist_secret_named_keys() -> None:
    """The options bag is plain settings, so secrets must be dropped from it."""
    profile = AuthProfile(
        method="oauth2",
        options={"client_id": "app-1", "client_secret": "must-not-persist"},
    )
    document = profile.to_dict()
    assert document["options"] == {"client_id": "app-1"}
    assert "must-not-persist" not in json.dumps(document)
    assert AuthProfile.from_dict(
        {"method": "oauth2", "options": {"client_secret": "leaked"}}
    ).options == {}


def test_auth_binding_round_trip() -> None:
    binding = AuthBinding(identity="profile-1", api_key="profile-2", disabled=True)
    restored = AuthBinding.from_dict(binding.to_dict())
    assert restored == binding


# -- manual_bearer / api_key headers ----------------------------------------


def test_manual_bearer_headers_use_bearer_prefix(tmp_path) -> None:
    manager = make_manager(tmp_path)
    profile = AuthProfile(method="manual_bearer")
    manager.set_secret("dev", profile, "raw-token-value")
    headers = manager.headers("dev", profile)
    assert headers == {"Authorization": "Bearer raw-token-value"}


def test_api_key_headers_use_configured_header_name(tmp_path) -> None:
    manager = make_manager(tmp_path)
    profile = AuthProfile(method="api_key", header_name="x-custom-key")
    manager.set_secret("dev", profile, "abc123")
    headers = manager.headers("dev", profile)
    assert headers == {"x-custom-key": "abc123"}


def test_persistent_static_secret_uses_real_encrypted_backend_and_reloads(tmp_path) -> None:
    profile = AuthProfile(method="api_key", header_name="X-Subscription", persistent=True)
    manager = make_manager(tmp_path)
    manager.set_secret("dev", profile, "remembered-api-key")

    secret_files = list((tmp_path / "auth-cache").glob("*.secret"))
    assert len(secret_files) == 1
    assert b"remembered-api-key" not in secret_files[0].read_bytes()

    restarted = make_manager(tmp_path)
    assert restarted.headers("dev", profile) == {"X-Subscription": "remembered-api-key"}

    restarted.clear_session("dev", profile)
    assert not list((tmp_path / "auth-cache").glob("*.secret"))
    with pytest.raises(AuthError, match="No API key"):
        restarted.headers("dev", profile)


def test_persistent_static_secret_failure_is_sanitized_and_not_retained(tmp_path) -> None:
    def failing_persistence(path):
        raise RuntimeError("secret=provider-sensitive DPAPI failure")

    manager = make_manager(tmp_path, persistence_factory=failing_persistence)
    profile = AuthProfile(method="api_key", persistent=True)

    with pytest.raises(AuthError, match="remember the credential securely") as excinfo:
        manager.set_secret("dev", profile, "must-not-remain")

    assert "provider-sensitive" not in str(excinfo.value)
    with pytest.raises(AuthError, match="No API key"):
        manager.headers("dev", AuthProfile(id=profile.id, method="api_key"))


def test_headers_without_secret_raise_auth_error(tmp_path) -> None:
    manager = make_manager(tmp_path)
    profile = AuthProfile(method="manual_bearer")
    with pytest.raises(AuthError):
        manager.headers("dev", profile)
    profile = AuthProfile(method="api_key")
    with pytest.raises(AuthError):
        manager.headers("dev", profile)


def test_set_secret_rejects_oauth_methods(tmp_path) -> None:
    manager = make_manager(tmp_path)
    profile = oauth_profile()
    with pytest.raises(ValueError, match="sign_in"):
        manager.set_secret("dev", profile, "whatever")


def test_set_secret_rejects_blank_value(tmp_path) -> None:
    manager = make_manager(tmp_path)
    profile = AuthProfile(method="manual_bearer")
    with pytest.raises(ValueError):
        manager.set_secret("dev", profile, "   ")


# -- entra_pkce / b2c_pkce headers (silent MSAL flow) -----------------------


def test_entra_headers_return_bearer_from_cached_silent_result(tmp_path) -> None:
    app = FakePublicClientApp("client")
    app.accounts = [{"username": "user@contoso.com", "home_account_id": "1"}]
    app.default_silent_result = {"access_token": "cached-token", "expires_in": 3600}
    manager = make_manager(tmp_path, public_app=app)
    profile = oauth_profile()

    headers = manager.headers("dev", profile)

    assert headers == {"Authorization": "Bearer cached-token"}
    assert app.interactive_calls == []  # headers() never opens a browser


def test_headers_never_triggers_interactive_sign_in(tmp_path) -> None:
    app = FakePublicClientApp("client")
    app.accounts = []
    manager = make_manager(tmp_path, public_app=app)
    profile = oauth_profile()

    with pytest.raises(InteractionRequired):
        manager.headers("dev", profile)

    assert app.interactive_calls == []


def test_entra_headers_raise_interaction_required_without_account(tmp_path) -> None:
    app = FakePublicClientApp("client")
    manager = make_manager(tmp_path, public_app=app)
    profile = oauth_profile()
    with pytest.raises(InteractionRequired):
        manager.headers("dev", profile)


def test_entra_headers_classify_interaction_required_error_code(tmp_path) -> None:
    app = FakePublicClientApp("client")
    app.accounts = [{"username": "user@contoso.com"}]
    app.default_silent_result = {"error": "interaction_required", "error_description": "secret detail"}
    manager = make_manager(tmp_path, public_app=app)
    profile = oauth_profile()

    with pytest.raises(InteractionRequired) as excinfo:
        manager.headers("dev", profile)
    assert "secret detail" not in str(excinfo.value)
    assert app.silent_with_error_calls == 1


def test_entra_headers_classify_invalid_grant_as_auth_error(tmp_path) -> None:
    app = FakePublicClientApp("client")
    app.accounts = [{"username": "user@contoso.com"}]
    app.default_silent_result = {"error": "invalid_grant", "error_description": "refresh token revoked"}
    manager = make_manager(tmp_path, public_app=app)
    profile = oauth_profile()

    with pytest.raises(AuthError) as excinfo:
        manager.headers("dev", profile)
    assert not isinstance(excinfo.value, InteractionRequired)
    assert "refresh token revoked" not in str(excinfo.value)


def test_entra_headers_proactively_refreshes_short_lived_token(tmp_path) -> None:
    app = FakePublicClientApp("client")
    app.accounts = [{"username": "user@contoso.com"}]
    # First silent call returns a token that expires in <=60s; the manager
    # should proactively ask for a forced refresh instead of handing it back.
    app.silent_results.append({"access_token": "short-lived", "expires_in": 30})
    app.silent_results.append({"access_token": "refreshed", "expires_in": 3600})
    manager = make_manager(tmp_path, public_app=app)
    profile = oauth_profile()

    headers = manager.headers("dev", profile)

    assert headers == {"Authorization": "Bearer refreshed"}
    assert len(app.silent_calls) == 2
    assert app.silent_calls[0]["force_refresh"] is False
    assert app.silent_calls[1]["force_refresh"] is True


def test_short_lived_refresh_preserves_interaction_required_error(tmp_path) -> None:
    app = FakePublicClientApp("client")
    app.accounts = [{"username": "user@contoso.com"}]
    app.silent_results.append({"access_token": "nearly-expired", "expires_in": 30})
    app.silent_results.append(
        {"error": "interaction_required", "error_description": "sensitive provider detail"}
    )
    manager = make_manager(tmp_path, public_app=app)

    with pytest.raises(InteractionRequired) as excinfo:
        manager.headers("dev", oauth_profile())

    assert "sensitive provider detail" not in str(excinfo.value)


def test_entra_headers_honors_explicit_force_refresh(tmp_path) -> None:
    app = FakePublicClientApp("client")
    app.accounts = [{"username": "user@contoso.com"}]
    app.default_silent_result = {"access_token": "fresh-token", "expires_in": 3600}
    manager = make_manager(tmp_path, public_app=app)
    profile = oauth_profile()

    manager.headers("dev", profile, force_refresh=True)

    assert app.silent_calls[0]["force_refresh"] is True


# -- sign_in() / renew() -----------------------------------------------------


def test_sign_in_rejects_non_interactive_methods(tmp_path) -> None:
    manager = make_manager(tmp_path)
    profile = AuthProfile(method="manual_bearer")
    with pytest.raises(ValueError, match="Sign In"):
        manager.sign_in("dev", profile)
    profile = AuthProfile(
        method="client_credentials",
        authority="https://login.microsoftonline.com/contoso-tenant",
        client_id="11111111-1111-1111-1111-111111111111",
        scopes=["api://app/.default"],
    )
    with pytest.raises(ValueError, match="Sign In"):
        manager.sign_in("dev", profile)


def test_sign_in_success_persists_cache(tmp_path) -> None:
    app = FakePublicClientApp("client")
    app.interactive_result = {"access_token": "new-token", "expires_in": 3600}
    cache = FakeCache()
    cache.has_state_changed = True
    manager = make_manager(tmp_path, public_app=app, cache=cache)
    profile = oauth_profile(persistent=False)

    manager.sign_in("dev", profile)

    assert app.interactive_calls == [
        {"scopes": profile.scopes, "timeout": profile.login_timeout, "port": None}
    ]
    # persistent=False: nothing should be written to disk, only kept in memory.
    assert not any((tmp_path / "auth-cache").glob("*.bin"))


def test_sign_in_pins_the_listener_port_from_the_redirect_uri(tmp_path) -> None:
    app = FakePublicClientApp("client")
    app.interactive_result = {"access_token": "new-token", "expires_in": 3600}
    manager = make_manager(tmp_path, public_app=app)
    profile = oauth_profile(persistent=False)
    profile.redirect_uri = "http://localhost:5000"

    manager.sign_in("dev", profile)

    assert app.interactive_calls[0]["port"] == 5000


def test_sign_in_leaves_the_port_system_allocated_without_a_redirect_uri(
    tmp_path,
) -> None:
    app = FakePublicClientApp("client")
    app.interactive_result = {"access_token": "new-token", "expires_in": 3600}
    manager = make_manager(tmp_path, public_app=app)
    profile = oauth_profile(persistent=False)
    profile.redirect_uri = "http://localhost"

    manager.sign_in("dev", profile)

    # A redirect URI with no port must not pin one; MSAL maps None to port 0.
    assert app.interactive_calls[0]["port"] is None


def test_redirect_uri_must_be_a_loopback_http_address_without_a_path() -> None:
    profile = oauth_profile()
    for candidate in (
        "https://localhost:5000",
        "http://127.0.0.1:5000",
        "http://example.com:5000",
        "http://localhost:80",
        "http://localhost:5000/callback",
        "http://localhost:5000?code=1",
    ):
        profile.redirect_uri = candidate
        with pytest.raises(ValueError):
            profile.validate()


def test_redirect_uri_is_rejected_for_non_interactive_methods() -> None:
    profile = oauth_profile()
    profile.method = "client_credentials"
    profile.redirect_uri = "http://localhost:5000"

    with pytest.raises(ValueError):
        profile.validate()


def test_redirect_uri_round_trips_and_defaults_to_empty_for_legacy_documents() -> None:
    profile = oauth_profile()
    profile.redirect_uri = "http://localhost:5000"

    assert AuthProfile.from_dict(profile.to_dict()).redirect_uri == (
        "http://localhost:5000"
    )
    assert AuthProfile.from_dict({"method": "entra_pkce"}).redirect_uri == ""


def test_sign_in_initializes_encrypted_cache_when_persistent(tmp_path) -> None:
    app = FakePublicClientApp("client")
    app.interactive_result = {"access_token": "new-token", "expires_in": 3600}
    manager = make_manager(
        tmp_path,
        public_app=app,
        persistence_factory=lambda path: object(),
        persisted_cache_factory=lambda persistence: FakeCache(),
    )
    profile = oauth_profile(persistent=True)

    manager.sign_in("dev", profile)

    assert app.interactive_calls
    assert not list((tmp_path / "auth-cache").glob("*.bin"))


def test_sign_in_classifies_interaction_error(tmp_path) -> None:
    app = FakePublicClientApp("client")
    app.interactive_result = {"error": "invalid_grant", "error_description": "leaked detail"}
    manager = make_manager(tmp_path, public_app=app)
    profile = oauth_profile()

    with pytest.raises(AuthError) as excinfo:
        manager.sign_in("dev", profile)
    assert "leaked detail" not in str(excinfo.value)


def test_sign_in_sanitizes_raw_provider_exceptions(tmp_path) -> None:
    app = FakePublicClientApp("client")
    app.interactive_exception = RuntimeError("token=super-secret-value at https://provider/x")
    manager = make_manager(tmp_path, public_app=app)
    profile = oauth_profile()

    with pytest.raises(AuthError) as excinfo:
        manager.sign_in("dev", profile)
    assert "super-secret-value" not in str(excinfo.value)
    assert "provider" not in str(excinfo.value)


def test_renew_rejects_manual_methods(tmp_path) -> None:
    manager = make_manager(tmp_path)
    with pytest.raises(ValueError):
        manager.renew("dev", AuthProfile(method="manual_bearer"))
    with pytest.raises(ValueError):
        manager.renew("dev", AuthProfile(method="api_key"))


def test_renew_forces_silent_refresh_for_oauth(tmp_path) -> None:
    app = FakePublicClientApp("client")
    app.accounts = [{"username": "user@contoso.com"}]
    app.default_silent_result = {"access_token": "renewed", "expires_in": 3600}
    manager = make_manager(tmp_path, public_app=app)
    profile = oauth_profile()

    manager.renew("dev", profile)

    assert app.silent_calls[-1]["force_refresh"] is True


# -- client_credentials --------------------------------------------------


def client_credentials_profile(**overrides):
    values = dict(
        method="client_credentials",
        authority="https://login.microsoftonline.com/contoso-tenant",
        client_id="22222222-2222-2222-2222-222222222222",
        scopes=["api://app/.default"],
    )
    values.update(overrides)
    return AuthProfile(**values)


def test_client_credentials_headers_success(tmp_path) -> None:
    confidential = FakeConfidentialClientApp("client")
    confidential.for_client_result = {"access_token": "app-only-token", "expires_in": 3600}
    manager = make_manager(tmp_path, confidential_app=confidential)
    profile = client_credentials_profile()
    manager.set_secret("dev", profile, "super-secret-client-secret")

    headers = manager.headers("dev", profile)

    assert headers == {"Authorization": "Bearer app-only-token"}
    assert confidential.client_credential == "super-secret-client-secret"


def test_client_credentials_headers_require_secret(tmp_path) -> None:
    manager = make_manager(tmp_path)
    profile = client_credentials_profile()
    with pytest.raises(AuthError, match="client secret"):
        manager.headers("dev", profile)


def test_client_credentials_force_refresh_evicts_cached_app_tokens(tmp_path) -> None:
    app = FakeConfidentialClientApp("client")
    manager = make_manager(tmp_path, confidential_app=app)
    profile = client_credentials_profile()
    manager.set_secret("dev", profile, "s3cret")

    manager.renew("dev", profile)

    assert app.remove_tokens_calls == 1
    assert app.calls == [{"scopes": profile.scopes}]


def test_set_secret_rebuilds_confidential_app_on_rotation(tmp_path) -> None:
    built_secrets = []

    def factory(kind, profile, cache, secret):
        built_secrets.append(secret)
        return FakeConfidentialClientApp("client", client_credential=secret)

    manager = AuthenticationManager(
        cache_root=tmp_path / "auth-cache",
        msal_app_factory=factory,
        cache_serializer_factory=lambda: FakeCache(),
    )
    profile = client_credentials_profile()
    manager.set_secret("dev", profile, "first-secret")
    manager.headers("dev", profile)
    manager.set_secret("dev", profile, "rotated-secret")
    manager.headers("dev", profile)

    assert built_secrets == ["first-secret", "rotated-secret"]


# -- status() --------------------------------------------------------------


def test_status_for_manual_methods(tmp_path) -> None:
    manager = make_manager(tmp_path)
    profile = AuthProfile(method="manual_bearer")
    assert manager.status("dev", profile) == "not signed in"
    manager.set_secret("dev", profile, "token")
    assert manager.status("dev", profile) == "configured"


def test_status_reports_not_configured_for_invalid_profile() -> None:
    manager = AuthenticationManager(cache_root=None)
    profile = oauth_profile(client_id="")
    assert manager.status("dev", profile) == "not configured"


def test_status_reports_signed_in_with_expiry(tmp_path) -> None:
    app = FakePublicClientApp("client")
    app.accounts = [{"username": "user@contoso.com"}]
    app.default_silent_result = {"access_token": "tok", "expires_in": 120}
    manager = make_manager(tmp_path, public_app=app)
    profile = oauth_profile()

    status = manager.status("dev", profile)

    assert status == "signed in as user@contoso.com (expires in 120s)"


def test_status_reports_not_signed_in_without_account(tmp_path) -> None:
    manager = make_manager(tmp_path)
    profile = oauth_profile()
    assert manager.status("dev", profile) == "not signed in"


def test_status_for_client_credentials(tmp_path) -> None:
    confidential = FakeConfidentialClientApp("client")
    confidential.for_client_result = {"access_token": "tok", "expires_in": 55}
    manager = make_manager(tmp_path, confidential_app=confidential)
    profile = client_credentials_profile()

    assert manager.status("dev", profile) == "not configured"
    manager.set_secret("dev", profile, "s3cret")
    assert manager.status("dev", profile) == "ready (expires in 55s)"


# -- clear_session() ---------------------------------------------------------


def test_clear_session_removes_secret_and_calls_remove_account_not_provider_logout(tmp_path) -> None:
    app = FakePublicClientApp("client")
    account = {"username": "user@contoso.com"}
    app.accounts = [account]
    app.default_silent_result = {"access_token": "tok", "expires_in": 3600}
    manager = make_manager(tmp_path, public_app=app)
    profile = oauth_profile()
    manager.headers("dev", profile)  # establishes a session

    manager.clear_session("dev", profile)

    assert app.removed_accounts == [account]
    assert manager.status("dev", profile) == "not signed in"
    # Only the local cache is touched: no HTTP logout endpoint exists on the fake.
    assert not hasattr(app, "logout")


def test_clear_session_deletes_persisted_cache_file(tmp_path) -> None:
    manager = make_manager(
        tmp_path,
        persistence_factory=lambda path: object(),
        persisted_cache_factory=lambda persistence: FakeCache(),
    )
    profile = oauth_profile(persistent=True)
    session_key = manager._session_key("dev", profile)
    cache_dir = tmp_path / "auth-cache"
    cache_dir.mkdir(parents=True)
    manager._cache_path(session_key).write_bytes(b"encrypted-cache")

    manager.clear_session("dev", profile)

    assert not list(cache_dir.glob("*.bin"))


def test_clear_session_forgets_manual_secret(tmp_path) -> None:
    manager = make_manager(tmp_path)
    profile = AuthProfile(method="manual_bearer")
    manager.set_secret("dev", profile, "token")
    manager.clear_session("dev", profile)
    with pytest.raises(AuthError):
        manager.headers("dev", profile)


def test_clear_session_removes_encrypted_state_for_previous_config(tmp_path) -> None:
    profile = AuthProfile(method="api_key", header_name="X-Original", persistent=True)
    manager = make_manager(tmp_path)
    manager.set_secret("dev", profile, "remembered")
    old_secret_path = manager._secret_path(manager._session_key("dev", profile))
    assert old_secret_path.exists()

    profile.header_name = "X-Renamed"
    manager.clear_session("dev", profile)

    assert not old_secret_path.exists()


def test_clear_session_evicts_previous_config_in_memory_session(tmp_path) -> None:
    app = FakePublicClientApp("client")
    account = {"username": "user@contoso.com"}
    app.accounts = [account]
    manager = make_manager(
        tmp_path,
        public_app=app,
        persistence_factory=lambda path: object(),
        persisted_cache_factory=lambda persistence: FakeCache(),
    )
    profile = oauth_profile(persistent=True)
    manager._get_or_create_session("dev", profile, secret=None)

    profile.authority = "https://login.microsoftonline.com/changed-tenant"
    manager.clear_session("dev", profile)

    assert app.removed_accounts == [account]
    assert manager._sessions == {}


# -- isolation across environments / profiles / config changes --------------


def test_session_isolation_across_environments_and_profiles(tmp_path) -> None:
    built = []

    def factory(kind, profile, cache, secret):
        app = FakePublicClientApp(profile.client_id)
        built.append((kind, id(cache)))
        return app

    manager = AuthenticationManager(
        cache_root=tmp_path / "auth-cache",
        msal_app_factory=factory,
        cache_serializer_factory=lambda: FakeCache(),
    )
    profile_a = oauth_profile()
    profile_b = oauth_profile()  # distinct id() -> distinct profile identity

    session_a1 = manager._get_or_create_session("dev", profile_a, secret=None)
    session_a2 = manager._get_or_create_session("prod", profile_a, secret=None)
    session_b = manager._get_or_create_session("dev", profile_b, secret=None)

    assert session_a1 is not session_a2  # environment isolation
    assert session_a1 is not session_b  # profile isolation
    assert len(built) == 3


def test_config_change_creates_new_session_and_invalidates_old_cache_file(tmp_path) -> None:
    manager = make_manager(
        tmp_path,
        persistence_factory=lambda path: object(),
        persisted_cache_factory=lambda persistence: FakeCache(),
    )
    profile = oauth_profile(persistent=True)
    old_key = manager._session_key("dev", profile)
    manager._get_or_create_session("dev", profile, secret=None)
    old_path = manager._cache_path(old_key)
    old_path.parent.mkdir(parents=True, exist_ok=True)
    old_path.write_text("stale-cache-bytes", encoding="utf-8")

    profile.scopes = ["api://app/.new-scope"]  # changes the fingerprint
    new_key = manager._session_key("dev", profile)
    assert new_key != old_key

    manager._get_or_create_session("dev", profile, secret=None)

    assert not old_path.exists()


# -- concurrency: single-flight per profile context --------------------------


def test_concurrent_headers_calls_are_single_flight(tmp_path) -> None:
    app = FakePublicClientApp("client")
    app.accounts = [{"username": "user@contoso.com"}]
    concurrent_calls = []
    max_concurrent = [0]
    call_lock = threading.Lock()

    original_silent = app.acquire_token_silent

    def tracking_silent(scopes, account=None, force_refresh=False):
        with call_lock:
            concurrent_calls.append(1)
            max_concurrent[0] = max(max_concurrent[0], len(concurrent_calls))
        time.sleep(0.05)  # widen the window so overlapping calls would show up
        try:
            return original_silent(scopes, account=account, force_refresh=force_refresh)
        finally:
            with call_lock:
                concurrent_calls.pop()

    app.acquire_token_silent = tracking_silent
    app.default_silent_result = {"access_token": "tok", "expires_in": 3600}
    manager = make_manager(tmp_path, public_app=app)
    profile = oauth_profile()

    errors = []

    def worker():
        try:
            manager.headers("dev", profile)
        except Exception as exc:  # pragma: no cover - failure surfaced via errors list
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=5)

    assert not errors
    assert max_concurrent[0] == 1  # never more than one in-flight silent call
    assert len(app.silent_calls) == 8


# -- sanitization -------------------------------------------------------------


def test_headers_sanitizes_raw_provider_exceptions(tmp_path) -> None:
    app = FakePublicClientApp("client")
    app.accounts = [{"username": "user@contoso.com"}]

    def boom(scopes, account=None, force_refresh=False):
        raise RuntimeError("client_secret=abcd1234 leaked in exception")

    app.acquire_token_silent = boom
    manager = make_manager(tmp_path, public_app=app)
    profile = oauth_profile()

    with pytest.raises(AuthError) as excinfo:
        manager.headers("dev", profile)
    assert "abcd1234" not in str(excinfo.value)
    assert "client_secret" not in str(excinfo.value)


def test_no_secret_ever_stored_on_auth_profile(tmp_path) -> None:
    manager = make_manager(tmp_path)
    profile = AuthProfile(method="manual_bearer")
    manager.set_secret("dev", profile, "top-secret-value")
    assert "top-secret-value" not in repr(profile)
    assert "top-secret-value" not in str(profile.to_dict())
    assert not hasattr(profile, "secret")


# -- storage resilience --------------------------------------------------------


def test_persistence_backend_failure_raises_sanitized_auth_error(tmp_path) -> None:
    def failing_persistence_factory(path):
        raise RuntimeError("DPAPI unavailable on this platform")

    app = FakePublicClientApp("client")
    app.interactive_result = {"access_token": "tok", "expires_in": 3600}
    manager = make_manager(
        tmp_path, public_app=app, persistence_factory=failing_persistence_factory
    )
    profile = oauth_profile(persistent=True)

    with pytest.raises(AuthError, match="Encrypted sign-in storage is unavailable") as excinfo:
        manager.sign_in("dev", profile)

    assert "DPAPI" not in str(excinfo.value)
    assert not list((tmp_path / "auth-cache").glob("*.bin"))


# -- singleton -----------------------------------------------------------------


def test_get_auth_manager_returns_singleton() -> None:
    first = get_auth_manager()
    second = get_auth_manager()
    assert first is second
    assert isinstance(first, AuthenticationManager)


def test_auth_profile_validate_accepts_mixed_case_hyphenated_api_key_headers() -> None:
    for header_name in (
        "X-Subscription",
        "X-Subscription-Key",
        "Ocp-Apim-Subscription-Key",
        "x-api-key",
        "X_Subscription_Key",
    ):
        AuthProfile(method="api_key", header_name=header_name).validate()  # does not raise


# -- real-MSAL signature compatibility (regression) --------------------------


def test_persistence_factory_calls_build_encrypted_persistence_with_location_only(tmp_path) -> None:
    """MSAL-extensions 1.3.1's ``build_encrypted_persistence(location)`` takes
    no ``fallback_to_plaintext`` kwarg; passing one would raise ``TypeError``
    on the real dependency. Guards against reintroducing that kwarg."""
    calls = []

    def real_shaped_persistence_factory(path):
        calls.append(path)
        return object()

    manager = make_manager(
        tmp_path,
        persistence_factory=real_shaped_persistence_factory,
        persisted_cache_factory=lambda persistence: FakeCache(),
    )
    profile = oauth_profile(persistent=True)

    manager._get_or_create_session("dev", profile, secret=None)

    assert len(calls) == 1  # called successfully with a single positional path


def test_installed_msal_exposes_required_public_refresh_apis() -> None:
    import msal

    assert hasattr(msal.PublicClientApplication, "acquire_token_silent_with_error")
    assert hasattr(msal.ConfidentialClientApplication, "remove_tokens_for_client")


def test_acquire_for_client_uses_modern_msal_refresh_api(tmp_path) -> None:
    app = FakeConfidentialClientApp("client")
    manager = make_manager(tmp_path, confidential_app=app)
    profile = client_credentials_profile()
    manager.set_secret("dev", profile, "s3cret")

    manager.renew("dev", profile)

    assert app.remove_tokens_calls == 1
    assert app.calls == [{"scopes": profile.scopes}]


def test_a_public_oauth_client_does_not_demand_an_optional_client_secret() -> None:
    """A PKCE public client has no secret; requiring one blocks every send.

    The guard used to fire whenever a method declared any secret field,
    so OAuth 2.0 authorization-code profiles were rejected before the
    cached token was ever consulted - while ``status()`` reported them
    signed in, because it already filtered on ``required``.
    """
    from api_tester.authentication import InteractionRequired

    manager = AuthenticationManager()
    profile = AuthProfile(
        id="o", name="Public", method="oauth2", client_id="public-client",
        options={
            "grant_type": "authorization_code",
            "token_url": "https://id.example.com/t",
            "authorization_url": "https://id.example.com/a",
            "redirect_uri": "http://localhost",
        },
    )

    with pytest.raises(InteractionRequired):
        manager.credentials("env", profile)


def test_oauth1_without_optional_secrets_is_not_rejected_as_unconfigured() -> None:
    from api_tester.auth_strategies import RequestContext

    manager = AuthenticationManager()
    profile = AuthProfile(
        id="o1", name="OAuth1", method="oauth1",
        options={"consumer_key": "ck", "signature_method": "HMAC-SHA1"},
    )
    context = RequestContext(method="GET", url="https://api.example.com/v1/items")
    outcome = manager.credentials("env", profile, context=context)

    assert any("OAuth" in value for value in outcome.headers.values())


def test_a_genuinely_missing_required_secret_is_still_reported() -> None:
    manager = AuthenticationManager()
    profile = AuthProfile(
        id="k", name="Key", method="api_key", header_name="x-api-key"
    )

    with pytest.raises(AuthError, match="API key"):
        manager.credentials("env", profile)


def test_missing_secret_message_names_only_required_fields() -> None:
    from api_tester.authentication import _missing_secret_message
    from api_tester.auth_strategies import get_strategy

    message = _missing_secret_message(get_strategy("basic"))

    assert "username" in message.lower()
    assert "password" not in message.lower()
