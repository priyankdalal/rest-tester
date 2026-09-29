"""Tests for the Authentication settings UI.

These tests are fully offline. They never talk to a real token store,
network, or browser: :class:`FakeAuthenticationManager` below is injected
into every widget under test via the ``manager=`` constructor argument, so
no code path here can trigger a live sign-in.

``api_tester.authentication`` is owned by another agent and may not exist
yet in this checkout. If it is importable, these tests use the real
dataclasses (``AuthProfile``/``AuthBinding``) from it, since those are pure
data and safe to construct directly. If it is not importable, a small
stand-in module conforming to the documented contract is installed into
``sys.modules`` so this file - and the ``api_tester.environment``,
``environment_ui``, and ``authentication_ui`` modules that import from it -
can be exercised standalone. Either way, the *manager* used by every test is
always the local fake, never whatever ``AuthenticationManager`` the real or
stand-in module provides.
"""

from __future__ import annotations

import sys
import time
import types
import uuid
from copy import deepcopy
from dataclasses import dataclass, field

import pytest


def _install_authentication_contract() -> None:
    if "api_tester.authentication" in sys.modules:
        return
    try:
        import api_tester.authentication  # noqa: F401
        return
    except ImportError:
        pass

    module = types.ModuleType("api_tester.authentication")

    class AuthError(RuntimeError):
        pass

    class InteractionRequired(AuthError):
        pass

    valid_methods = (
        "manual_bearer", "api_key", "entra_pkce", "b2c_pkce", "client_credentials"
    )
    interactive_methods = ("entra_pkce", "b2c_pkce", "client_credentials")

    @dataclass
    class AuthProfile:
        id: str = field(default_factory=lambda: str(uuid.uuid4()))
        name: str = "Authentication"
        method: str = "manual_bearer"
        authority: str = ""
        client_id: str = ""
        scopes: list = field(default_factory=list)
        header_name: str = "x-api-key"
        persistent: bool = False
        login_timeout: int = 120

        @classmethod
        def from_dict(cls, value):
            return cls(
                id=str(value.get("id") or uuid.uuid4()),
                name=str(value.get("name", "Authentication")),
                method=str(value.get("method", "manual_bearer")),
                authority=str(value.get("authority", "")),
                client_id=str(value.get("client_id", "")),
                scopes=[str(item) for item in value.get("scopes", [])],
                header_name=str(value.get("header_name", "x-api-key")),
                persistent=bool(value.get("persistent", False)),
                login_timeout=int(value.get("login_timeout", 120)),
            )

        def to_dict(self):
            return {
                "id": self.id,
                "name": self.name,
                "method": self.method,
                "authority": self.authority,
                "client_id": self.client_id,
                "scopes": list(self.scopes),
                "header_name": self.header_name,
                "persistent": self.persistent,
                "login_timeout": self.login_timeout,
            }

        def validate(self):
            if self.method not in valid_methods:
                raise ValueError(f"Unknown authentication method: {self.method}")
            if not self.name.strip():
                raise ValueError("Authentication profile name is required.")
            if self.method in interactive_methods:
                if not self.authority.strip():
                    raise ValueError("Authority is required for this method.")
                if not self.client_id.strip():
                    raise ValueError("Client ID is required for this method.")
            if self.method == "api_key" and not self.header_name.strip():
                raise ValueError("Header name is required for API key profiles.")
            if not 1 <= self.login_timeout <= 3600:
                raise ValueError("Sign-in timeout must be between 1 and 3600 seconds.")

    @dataclass
    class AuthBinding:
        identity: str = ""
        api_key: str = ""
        disabled: bool = False

        @classmethod
        def from_dict(cls, value):
            return cls(
                identity=str(value.get("identity", "")),
                api_key=str(value.get("api_key", "")),
                disabled=bool(value.get("disabled", False)),
            )

        def to_dict(self):
            return {
                "identity": self.identity,
                "api_key": self.api_key,
                "disabled": self.disabled,
            }

    class AuthenticationManager:
        """Minimal stand-in; tests never use this - they inject a fake."""

        def set_secret(self, environment_id, profile, secret):
            raise NotImplementedError

        def set_secrets(self, environment_id, profile, values):
            raise NotImplementedError

        def headers(self, environment_id, profile, force_refresh=False):
            raise NotImplementedError

        def sign_in(self, environment_id, profile):
            raise NotImplementedError

        def renew(self, environment_id, profile):
            raise NotImplementedError

        def clear_session(self, environment_id, profile):
            raise NotImplementedError

        def status(self, environment_id, profile):
            return "Not signed in"

    _singleton: dict[str, AuthenticationManager] = {}

    def get_auth_manager() -> AuthenticationManager:
        if "instance" not in _singleton:
            _singleton["instance"] = AuthenticationManager()
        return _singleton["instance"]

    module.AuthError = AuthError
    module.InteractionRequired = InteractionRequired
    module.AuthProfile = AuthProfile
    module.AuthBinding = AuthBinding
    module.AuthenticationManager = AuthenticationManager
    module.get_auth_manager = get_auth_manager
    sys.modules["api_tester.authentication"] = module


_install_authentication_contract()

from api_tester.authentication import (  # noqa: E402
    AuthBinding,
    AuthError,
    AuthProfile,
)
from api_tester.environment import EnvironmentProfile  # noqa: E402


class FakeAuthenticationManager:
    """Records calls; never touches the network, a browser, or disk."""

    def __init__(self) -> None:
        self.secrets: dict[tuple[str, str], str] = {}
        self.sessions: dict[tuple[str, str], str] = {}
        self.calls: list[tuple] = []
        self.sign_in_gate = None
        self.fail_with: Exception | None = None

    def set_secret(self, environment_id, profile, secret):
        self.set_secrets(environment_id, profile, {"secret": secret})

    def set_secrets(self, environment_id, profile, values):
        # Recorded under the same name as the single-value entry point: both
        # are the one logical "store this credential" operation the UI tests
        # assert on, positively and negatively.
        self.calls.append(("set_secret", environment_id, profile.id))
        stored = list(values.values())
        self.secrets[(environment_id, profile.id)] = (
            stored[0] if len(stored) == 1 else dict(values)
        )

    def headers(self, environment_id, profile, force_refresh=False):
        self.calls.append(("headers", environment_id, profile.id))
        return {"Authorization": "Bearer fake-token"}

    def sign_in(self, environment_id, profile):
        self.calls.append(("sign_in", environment_id, profile.id))
        if self.sign_in_gate is not None:
            self.sign_in_gate.wait(5)
        if self.fail_with is not None:
            raise self.fail_with
        self.sessions[(environment_id, profile.id)] = "Signed in"

    def renew(self, environment_id, profile):
        self.calls.append(("renew", environment_id, profile.id))
        self.sessions[(environment_id, profile.id)] = "Signed in"

    def clear_session(self, environment_id, profile):
        self.calls.append(("clear_session", environment_id, profile.id))
        self.sessions.pop((environment_id, profile.id), None)
        self.secrets.pop((environment_id, profile.id), None)

    def status(self, environment_id, profile):
        return self.sessions.get((environment_id, profile.id), "Not signed in")


@pytest.fixture(scope="module")
def qt_app():
    pytest.importorskip("PyQt6")
    from PyQt6.QtWidgets import QApplication

    return QApplication.instance() or QApplication([])


@pytest.fixture()
def no_dialogs(monkeypatch):
    """Prevents QMessageBox popups from blocking headless test runs."""
    from PyQt6.QtWidgets import QMessageBox

    monkeypatch.setattr(QMessageBox, "information", lambda *a, **k: None)
    monkeypatch.setattr(QMessageBox, "warning", lambda *a, **k: None)
    monkeypatch.setattr(
        QMessageBox, "question", lambda *a, **k: QMessageBox.StandardButton.Yes
    )
    return QMessageBox


def make_profile(**overrides) -> EnvironmentProfile:
    defaults = dict(name="Development", base_urls={"Reporting": "https://r.example"})
    defaults.update(overrides)
    return EnvironmentProfile(**defaults)


# --------------------------------------------------------------------------
# EnvironmentProfile model: round trip, legacy migration, cloning
# --------------------------------------------------------------------------

def test_environment_profile_gets_a_stable_id_by_default() -> None:
    profile = make_profile()
    assert profile.id
    other = make_profile()
    assert other.id != profile.id


def test_auth_profiles_and_bindings_round_trip_through_to_dict_from_dict() -> None:
    auth_profile = AuthProfile(name="Reporting bearer", method="manual_bearer")
    profile = make_profile(
        auth_profiles={auth_profile.id: auth_profile},
        auth_bindings={"Reporting": AuthBinding(identity=auth_profile.id)},
    )
    document = profile.to_dict()

    restored = EnvironmentProfile.from_dict("Development", document)

    assert restored.id == profile.id
    assert set(restored.auth_profiles) == {auth_profile.id}
    assert restored.auth_profiles[auth_profile.id].name == "Reporting bearer"
    assert restored.auth_profiles[auth_profile.id].method == "manual_bearer"
    assert restored.auth_bindings["Reporting"].identity == auth_profile.id


def test_environment_id_is_stable_across_a_second_round_trip() -> None:
    profile = make_profile()
    first_id = profile.id
    restored = EnvironmentProfile.from_dict("Development", profile.to_dict())
    assert restored.id == first_id
    again = EnvironmentProfile.from_dict("Development", restored.to_dict())
    assert again.id == first_id


def test_legacy_environment_without_id_or_auth_fields_migrates_cleanly() -> None:
    legacy_document = {
        "base_urls": {"Reporting": "http://localhost:5262"},
        "verify_ssl": False,
        "request_timeout": 90,
        "custom_headers": {"Authorization": "Bearer legacy-token"},
    }

    restored = EnvironmentProfile.from_dict("Development", legacy_document)

    # A fresh, stable id is minted; existing manual-header credentials and
    # every other legacy field keep working exactly as before.
    assert restored.id
    assert restored.custom_headers == {"Authorization": "Bearer legacy-token"}
    assert restored.access_token == "Bearer legacy-token"
    assert restored.verify_ssl is False
    assert restored.request_timeout == 90
    assert restored.auth_profiles == {}
    assert restored.auth_bindings == {}


def test_clone_gets_a_fresh_id_and_deep_copied_auth_configuration() -> None:
    auth_profile = AuthProfile(name="QA identity", method="entra_pkce")
    source = make_profile(
        name="QA",
        auth_profiles={auth_profile.id: auth_profile},
        auth_bindings={"Reporting": AuthBinding(identity=auth_profile.id)},
    )

    clone = source.clone("QA Copy")

    assert clone.id != source.id
    assert clone.name == "QA Copy"
    assert set(clone.auth_profiles) == set(source.auth_profiles)
    assert clone.auth_profiles[auth_profile.id].name == "QA identity"
    assert clone.auth_bindings["Reporting"].identity == auth_profile.id

    # Deep copy: mutating the clone must never affect the source.
    clone.auth_profiles[auth_profile.id].name = "Renamed on clone"
    clone.auth_bindings["Reporting"].disabled = True
    assert source.auth_profiles[auth_profile.id].name == "QA identity"
    assert source.auth_bindings["Reporting"].disabled is False


def test_clone_preserves_existing_positional_construction_call() -> None:
    # api_tester.environment_ui previously built clones positionally; the new
    # fields must all have defaults so that call shape still works.
    profile = EnvironmentProfile(
        "QA",
        {"Reporting": "https://qa.example"},
        False,
        60,
        {"trialId": "470"},
        {"X-Tenant": "local"},
    )
    assert profile.id
    assert profile.auth_profiles == {}
    assert profile.auth_bindings == {}


def test_custom_headers_and_secrets_are_unaffected_by_auth_fields() -> None:
    """Backward compatibility: existing manual header credentials are
    untouched by the new managed-authentication fields."""
    profile = make_profile(custom_headers={"x-api-key": "legacy-key"})
    document = profile.to_dict()
    assert document["custom_headers"] == {"x-api-key": "legacy-key"}
    restored = EnvironmentProfile.from_dict("Development", document)
    assert restored.api_key == "legacy-key"


# --------------------------------------------------------------------------
# AuthenticationSettings widget: editing, validation, bindings
# --------------------------------------------------------------------------

@pytest.fixture()
def widget(qt_app):
    from api_tester.authentication_ui import AuthenticationSettings

    manager = FakeAuthenticationManager()
    profile = make_profile(base_urls={"Reporting": "https://r.example", "TrialAuth": ""})
    instance = AuthenticationSettings(
        profile, ["Reporting", "TrialAuth"], manager=manager
    )
    try:
        yield instance, manager, profile
    finally:
        instance.stop()


def _accept_dialog_with(dialog, monkeypatch, **field_values) -> None:
    """Fills an AuthProfileDialog's fields and accepts it without showing it."""
    from PyQt6.QtWidgets import QDialog

    if "name" in field_values:
        dialog.name_input.setText(field_values["name"])
    if "method" in field_values:
        index = dialog.method_input.findData(field_values["method"])
        dialog.method_input.setCurrentIndex(index)
    if "authority" in field_values:
        dialog.authority_input.setText(field_values["authority"])
    if "client_id" in field_values:
        dialog.client_id_input.setText(field_values["client_id"])
    if "scopes" in field_values:
        dialog.scopes_input.setText(field_values["scopes"])
    if "header_name" in field_values:
        dialog.header_name_input.setText(field_values["header_name"])
    if "secret" in field_values:
        dialog.secret_input.setText(field_values["secret"])
    monkeypatch.setattr(
        dialog, "exec", lambda: (dialog._accept(), QDialog.DialogCode.Accepted)[1]
    )


def test_authentication_actions_use_theme_aware_icons(widget, qt_app) -> None:
    from api_tester import theme

    instance, _manager, _profile = widget
    profile_buttons = (
        instance.add_button,
        instance.edit_button,
        instance.remove_button,
    )
    session_buttons = [
        *instance._sign_in_buttons.values(),
        *instance._renew_buttons.values(),
        *instance._clear_buttons.values(),
    ]
    buttons = [*profile_buttons, *session_buttons]

    assert all(button.text() == "" for button in buttons)
    assert all(button.toolTip() for button in buttons)
    assert all(button.accessibleName() == button.toolTip() for button in buttons)
    assert all(not button.icon().isNull() for button in buttons)
    assert instance.remove_button.property("danger") is True

    theme.apply_theme(qt_app, "Light")
    instance.refresh_theme()
    light_keys = [button.icon().cacheKey() for button in buttons]
    theme.apply_theme(qt_app, "Dark")
    instance.refresh_theme()
    assert [button.icon().cacheKey() for button in buttons] != light_keys
    theme.apply_theme(qt_app, "Light")


def test_binding_columns_are_resizable_and_session_actions_fit(widget, qt_app) -> None:
    from PyQt6.QtWidgets import QHeaderView, QPushButton

    instance, _manager, _profile = widget
    table = instance.bindings_table
    header = table.horizontalHeader()

    assert all(
        header.sectionResizeMode(column) == QHeaderView.ResizeMode.Interactive
        for column in range(table.columnCount())
    )
    assert table.columnWidth(5) >= 150
    assert table.verticalHeader().defaultSectionSize() >= 36

    instance.resize(950, 600)
    instance.show()
    qt_app.processEvents()
    actions = table.cellWidget(0, 5)
    buttons = actions.findChildren(QPushButton)
    assert len(buttons) == 3
    assert all(button.width() >= 32 for button in buttons)
    assert sum(button.width() for button in buttons) <= actions.width()


def test_add_profile_stores_secret_and_lists_it(widget, monkeypatch, no_dialogs) -> None:
    from api_tester import authentication_ui

    instance, manager, profile = widget
    real_dialog_class = authentication_ui.AuthProfileDialog
    created = {}

    def fake_dialog(existing, parent):
        dialog = real_dialog_class(existing, parent)
        _accept_dialog_with(
            dialog, monkeypatch,
            name="Reporting bearer", method="manual_bearer", secret="s3cr3t",
        )
        created["dialog"] = dialog
        return dialog

    monkeypatch.setattr(authentication_ui, "AuthProfileDialog", fake_dialog)
    instance._add_profile()

    assert instance.profile_list.count() == 1
    [profile_id] = list(instance._profiles)
    assert instance._profiles[profile_id].name == "Reporting bearer"
    assert manager.secrets[(profile.id, profile_id)] == "s3cr3t"
    assert profile_id in instance._new_profile_ids


def test_editing_a_profile_with_a_blank_secret_leaves_the_stored_secret_unchanged(
    widget, monkeypatch, no_dialogs
) -> None:
    from api_tester import authentication_ui

    instance, manager, profile = widget
    real_dialog_class = authentication_ui.AuthProfileDialog
    existing = AuthProfile(name="Key", method="api_key", header_name="x-api-key")
    instance._profiles[existing.id] = existing
    manager.secrets[(profile.id, existing.id)] = "original-key"
    instance._refresh_profile_list()
    instance.profile_list.setCurrentRow(0)

    def fake_dialog(current, parent):
        dialog = real_dialog_class(current, parent)
        _accept_dialog_with(dialog, monkeypatch, name="Key", method="api_key")
        return dialog

    monkeypatch.setattr(authentication_ui, "AuthProfileDialog", fake_dialog)
    instance._edit_profile()

    assert manager.secrets[(profile.id, existing.id)] == "original-key"
    assert ("set_secret", profile.id, existing.id) not in manager.calls


def test_add_profile_shows_a_message_when_set_secret_rejects_the_value(
    widget, monkeypatch, no_dialogs
) -> None:
    """`set_secret` on the real manager can raise `ValueError` (e.g. a
    profile that fails `validate()`), not just `AuthError`. The UI must
    surface that instead of letting it escape `_add_profile`.
    """
    from api_tester import authentication_ui

    instance, manager, profile = widget
    real_dialog_class = authentication_ui.AuthProfileDialog

    def raising_set_secret(environment_id, prof, values):
        raise ValueError("secret rejected by manager")

    monkeypatch.setattr(manager, "set_secrets", raising_set_secret)

    warnings = []
    monkeypatch.setattr(
        authentication_ui.QMessageBox,
        "warning",
        lambda *a, **k: warnings.append(a) or None,
    )

    def fake_dialog(existing, parent):
        dialog = real_dialog_class(existing, parent)
        _accept_dialog_with(
            dialog, monkeypatch,
            name="Reporting bearer", method="manual_bearer", secret="s3cr3t",
        )
        return dialog

    monkeypatch.setattr(authentication_ui, "AuthProfileDialog", fake_dialog)

    # Must not raise: the ValueError is caught and shown, not propagated.
    instance._add_profile()

    assert instance.profile_list.count() == 1
    assert warnings, "expected a warning dialog when set_secret raises ValueError"


def test_invalid_profile_raises_in_apply_before_touching_the_real_environment(
    widget,
) -> None:
    instance, manager, profile = widget
    bad = AuthProfile(name="Broken", method="entra_pkce", authority="", client_id="")
    instance._profiles[bad.id] = bad

    with pytest.raises(ValueError):
        instance.apply(profile)

    # apply() must fail atomically: nothing should have been written yet.
    assert profile.auth_profiles == {}


def test_identity_choices_exclude_api_key_profiles_and_vice_versa(widget) -> None:
    instance, manager, profile = widget
    bearer = AuthProfile(name="Bearer", method="manual_bearer")
    key = AuthProfile(name="Key", method="api_key")
    instance._profiles[bearer.id] = bearer
    instance._profiles[key.id] = key
    instance._refresh_binding_choices()

    identity_combo = instance._identity_combos["Reporting"]
    api_key_combo = instance._api_key_combos["Reporting"]
    identity_values = {identity_combo.itemData(i) for i in range(identity_combo.count())}
    api_key_values = {api_key_combo.itemData(i) for i in range(api_key_combo.count())}

    assert bearer.id in identity_values
    assert key.id not in identity_values
    assert key.id in api_key_values
    assert bearer.id not in api_key_values


def test_apply_rejects_an_identity_binding_pointed_at_an_api_key_profile(widget) -> None:
    instance, manager, profile = widget
    key = AuthProfile(name="Key", method="api_key")
    instance._profiles[key.id] = key
    instance._bindings["Reporting"].identity = key.id

    with pytest.raises(ValueError):
        instance.apply(profile)


def test_apply_rejects_an_api_key_binding_pointed_at_a_non_api_key_profile(widget) -> None:
    instance, manager, profile = widget
    bearer = AuthProfile(name="Bearer", method="manual_bearer")
    instance._profiles[bearer.id] = bearer
    instance._bindings["Reporting"].api_key = bearer.id

    with pytest.raises(ValueError):
        instance.apply(profile)


def test_valid_binding_is_written_back_only_through_apply(widget) -> None:
    instance, manager, profile = widget
    bearer = AuthProfile(name="Bearer", method="manual_bearer")
    instance._profiles[bearer.id] = bearer
    instance._bindings["Reporting"].identity = bearer.id

    assert profile.auth_profiles == {}  # untouched before apply()
    instance.apply(profile)

    assert profile.auth_profiles[bearer.id].name == "Bearer"
    assert profile.auth_bindings["Reporting"].identity == bearer.id


def test_custom_header_conflicting_with_managed_authorization_is_surfaced(
    widget,
) -> None:
    instance, manager, profile = widget
    instance.set_custom_header_names(["Authorization"])
    bearer = AuthProfile(name="Bearer", method="manual_bearer")
    instance._profiles[bearer.id] = bearer
    instance._bindings["Reporting"].identity = bearer.id
    instance._update_conflict_warning()

    assert "Reporting" in instance.conflict_warning.text()
    assert "authorization" in instance.conflict_warning.text().lower()


def test_no_conflict_warning_when_headers_and_bindings_do_not_overlap(widget) -> None:
    instance, manager, profile = widget
    instance.set_custom_header_names(["X-Tenant"])
    assert instance.conflict_warning.text() == ""




# --------------------------------------------------------------------------
# Profile removal / discard side effects
# --------------------------------------------------------------------------

def test_removing_a_profile_clears_its_session_and_unbinds_it(
    widget, no_dialogs
) -> None:
    instance, manager, profile = widget
    identity = AuthProfile(name="Bearer", method="manual_bearer")
    instance._profiles[identity.id] = identity
    instance._bindings["Reporting"].identity = identity.id
    manager.sessions[(profile.id, identity.id)] = "Signed in"
    instance._refresh_profile_list()
    instance.profile_list.setCurrentRow(0)

    instance._remove_profile()

    assert identity.id not in instance._profiles
    assert instance._bindings["Reporting"].identity == ""
    assert (profile.id, identity.id) not in manager.sessions
    assert ("clear_session", profile.id, identity.id) in manager.calls


def test_discard_clears_sessions_only_for_profiles_added_in_this_edit(widget) -> None:
    instance, manager, profile = widget
    pre_existing = AuthProfile(name="Existing", method="manual_bearer")
    instance._profiles[pre_existing.id] = pre_existing  # simulates an already-saved profile
    new_profile = AuthProfile(name="New", method="manual_bearer")
    instance._profiles[new_profile.id] = new_profile
    instance._new_profile_ids.add(new_profile.id)
    manager.set_secret(profile.id, pre_existing, "kept")
    manager.set_secret(profile.id, new_profile, "orphaned")

    instance.discard()

    assert ("clear_session", profile.id, new_profile.id) in manager.calls
    assert ("clear_session", profile.id, pre_existing.id) not in manager.calls
    assert (profile.id, pre_existing.id) in manager.secrets
    assert (profile.id, new_profile.id) not in manager.secrets


# --------------------------------------------------------------------------
# Sign in / renew: runs off the GUI thread, does not block, updates status
# --------------------------------------------------------------------------

def _pump_until(qt_app, condition, timeout=5.0) -> None:
    deadline = time.monotonic() + timeout
    while not condition() and time.monotonic() < deadline:
        qt_app.processEvents()
        time.sleep(0.005)
    assert condition(), "condition was never satisfied before the timeout"


def test_sign_in_runs_on_a_worker_thread_and_does_not_block_the_caller(
    qt_app, widget, no_dialogs
) -> None:
    import threading

    instance, manager, profile = widget
    identity = AuthProfile(name="Bearer", method="entra_pkce",
                            authority="https://login.example/tenant", client_id="abc")
    instance._profiles[identity.id] = identity
    instance._bindings["Reporting"].identity = identity.id
    instance._refresh_binding_choices()

    manager.sign_in_gate = threading.Event()
    started = time.monotonic()
    instance._sign_in("Reporting")

    # The call returns immediately even though sign_in() is still blocked -
    # proof the work happened on a thread, not the caller.
    assert time.monotonic() - started < 1.0
    assert instance.is_busy()
    assert not instance._sign_in_buttons["Reporting"].isEnabled()

    manager.sign_in_gate.set()
    _pump_until(qt_app, lambda: not instance.is_busy())

    assert ("sign_in", profile.id, identity.id) in manager.calls
    assert instance._status_labels["Reporting"].text() == "Signed in"


def test_failed_sign_in_reports_the_error_and_leaves_the_widget_usable(
    qt_app, widget, no_dialogs
) -> None:
    from api_tester.authentication import AuthError

    instance, manager, profile = widget
    identity = AuthProfile(name="Bearer", method="entra_pkce",
                            authority="https://login.example/tenant", client_id="abc")
    instance._profiles[identity.id] = identity
    instance._bindings["Reporting"].identity = identity.id
    instance._refresh_binding_choices()
    manager.fail_with = AuthError("sign-in cancelled")

    instance._sign_in("Reporting")
    _pump_until(qt_app, lambda: not instance.is_busy())

    assert instance._sign_in_buttons["Reporting"].isEnabled()


def test_clear_session_button_calls_manager_without_a_worker_thread(
    widget, no_dialogs
) -> None:
    instance, manager, profile = widget
    identity = AuthProfile(name="Bearer", method="manual_bearer")
    instance._profiles[identity.id] = identity
    instance._bindings["Reporting"].identity = identity.id
    manager.sessions[(profile.id, identity.id)] = "Signed in"

    instance._clear_session("Reporting")

    assert (profile.id, identity.id) not in manager.sessions
    assert instance._status_labels["Reporting"].text() == "Not signed in"


# --------------------------------------------------------------------------
# EnvironmentEditor integration: Authentication tab, save/cancel, busy-blocking
# --------------------------------------------------------------------------

@pytest.fixture()
def editor(qt_app, monkeypatch):
    from api_tester import environment_ui

    manager = FakeAuthenticationManager()
    monkeypatch.setattr(
        environment_ui, "AuthenticationSettings",
        lambda profile, service_names, parent=None: (
            __import__("api_tester.authentication_ui", fromlist=["AuthenticationSettings"])
            .AuthenticationSettings(profile, service_names, parent, manager=manager)
        ),
    )
    profile = make_profile(base_urls={"Reporting": "https://r.example"})
    instance = environment_ui.EnvironmentEditor(profile, ["Reporting"])
    try:
        yield instance, manager, profile
    finally:
        instance.auth_settings.stop()


def test_environment_editor_exposes_an_authentication_tab(editor) -> None:
    instance, manager, profile = editor
    from api_tester.authentication_ui import AuthenticationSettings

    assert isinstance(instance.auth_settings, AuthenticationSettings)
    # It edits a draft tied to the same environment id passed in.
    assert instance.auth_settings._environment_id == profile.id


def test_environment_editor_sections_use_a_vertical_rail_not_a_tab_strip(
    editor,
) -> None:
    from PyQt6.QtWidgets import QTabWidget

    instance, _manager, _profile = editor

    titles = [
        instance.section_rail.item(row).text()
        for row in range(instance.section_rail.count())
    ]
    assert titles == [
        "Base URLs",
        "Variables",
        "Custom Headers",
        "Transport",
        "Authentication",
    ]
    assert instance.sections.count() == len(titles)
    # The rail stacks vertically, so every row sits below the previous one.
    rects = [
        instance.section_rail.visualItemRect(instance.section_rail.item(row))
        for row in range(instance.section_rail.count())
    ]
    assert all(
        later.top() >= earlier.bottom() for earlier, later in zip(rects, rects[1:])
    )
    # Only the inner per-service strip on the Base URLs page stays a tab widget.
    outer_tabs = [
        widget
        for widget in instance.findChildren(QTabWidget)
        if widget.parent() is instance
    ]
    assert outer_tabs == []


def test_environment_editor_rail_selection_switches_the_visible_section(
    editor,
) -> None:
    instance, _manager, _profile = editor

    assert instance.sections.currentIndex() == 0

    instance.section_rail.setCurrentRow(4)

    assert instance.sections.currentIndex() == 4
    assert instance.sections.currentWidget() is instance.auth_settings


def test_environment_editor_rail_rows_carry_icons_and_tooltips(editor) -> None:
    instance, _manager, _profile = editor

    for row in range(instance.section_rail.count()):
        item = instance.section_rail.item(row)
        assert not item.icon().isNull()
        assert item.toolTip() == item.text()


def test_save_writes_auth_profiles_from_the_authentication_tab(editor, no_dialogs) -> None:
    instance, manager, profile = editor
    bearer = AuthProfile(name="Bearer", method="manual_bearer")
    instance.auth_settings._profiles[bearer.id] = bearer
    instance.auth_settings._bindings["Reporting"].identity = bearer.id

    instance._accept()

    assert profile.auth_profiles[bearer.id].name == "Bearer"
    assert profile.auth_bindings["Reporting"].identity == bearer.id
    assert instance.result() == instance.DialogCode.Accepted


def test_invalid_authentication_draft_blocks_save_and_keeps_dialog_open(
    editor, no_dialogs
) -> None:
    instance, manager, profile = editor
    broken = AuthProfile(name="Broken", method="entra_pkce", authority="", client_id="")
    instance.auth_settings._profiles[broken.id] = broken

    instance._accept()

    assert instance.result() != instance.DialogCode.Accepted
    assert profile.auth_profiles == {}


def test_cancelling_the_editor_never_persists_draft_auth_changes(editor, no_dialogs) -> None:
    instance, manager, profile = editor
    bearer = AuthProfile(name="Bearer", method="manual_bearer")
    instance.auth_settings._profiles[bearer.id] = bearer
    instance.auth_settings._new_profile_ids.add(bearer.id)
    manager.set_secret(profile.id, bearer, "s3cr3t")

    instance.reject()

    assert profile.auth_profiles == {}
    assert (profile.id, bearer.id) not in manager.secrets  # discard() cleaned it up


def test_editor_blocks_save_while_a_sign_in_is_running(qt_app, editor, no_dialogs) -> None:
    import threading

    instance, manager, profile = editor
    identity = AuthProfile(name="Bearer", method="entra_pkce",
                            authority="https://login.example/tenant", client_id="abc")
    instance.auth_settings._profiles[identity.id] = identity
    instance.auth_settings._bindings["Reporting"].identity = identity.id
    instance.auth_settings._refresh_binding_choices()
    manager.sign_in_gate = threading.Event()
    instance.auth_settings._sign_in("Reporting")

    try:
        instance._accept()
        assert instance.result() != instance.DialogCode.Accepted

        instance.reject()
        assert instance.result() != instance.DialogCode.Accepted
    finally:
        manager.sign_in_gate.set()
        _pump_until(qt_app, lambda: not instance.auth_settings.is_busy())


# --------------------------------------------------------------------------
# EnvironmentManagerPage: clone deep-copies auth config, no secrets/session
# --------------------------------------------------------------------------

def test_clone_environment_deep_copies_auth_config_and_warns(qt_app, monkeypatch) -> None:
    from api_tester.environment import AppSettings
    from api_tester import environment_ui

    warnings = []
    monkeypatch.setattr(
        environment_ui.QMessageBox, "information",
        lambda parent, title, text: warnings.append(text),
    )
    monkeypatch.setattr(
        environment_ui.QInputDialog, "getText",
        lambda *a, **k: ("QA Copy", True),
    )

    identity = AuthProfile(name="Identity", method="manual_bearer")
    source = make_profile(name="QA", auth_profiles={identity.id: identity})
    settings = AppSettings(environments={"QA": source}, active_environment="QA")
    page = environment_ui.EnvironmentManagerPage(settings, ["Reporting"])
    page.table.selectRow(0)

    page.clone()

    assert "QA Copy" in settings.environments
    clone = settings.environments["QA Copy"]
    assert clone.id != source.id
    assert clone.auth_profiles[identity.id].name == "Identity"
    assert warnings and "QA Copy" in warnings[0]


# --------------------------------------------------------------------------
# EnvironmentManagerPage: per-row Use checkbox + clone/edit/delete actions
# --------------------------------------------------------------------------

def _manager_page(qt_app, names=("Development", "QA"), active="Development"):
    from api_tester.environment import AppSettings
    from api_tester import environment_ui

    settings = AppSettings(
        environments={name: make_profile(name=name) for name in names},
        active_environment=active,
    )
    return settings, environment_ui.EnvironmentManagerPage(settings, ["Reporting"])


def test_each_row_exposes_use_checkbox_and_row_actions(qt_app) -> None:
    settings, page = _manager_page(qt_app)

    assert page.table.rowCount() == 2
    assert set(page._use_boxes) == {"Development", "QA"}
    assert set(page._clone_buttons) == {"Development", "QA"}
    assert set(page._delete_buttons) == {"Development", "QA"}
    # The active row is latched on so it can never be unchecked into a state
    # where no environment is active, but it stays enabled so it renders as a
    # normal ticked box rather than a greyed-out one.
    assert page._use_boxes["Development"].isChecked() is True
    assert page._use_boxes["Development"].isEnabled() is True
    assert page._use_boxes["QA"].isChecked() is False
    assert page._use_boxes["QA"].isEnabled() is True


def test_unticking_the_active_row_reverts_instead_of_unbinding(qt_app) -> None:
    settings, page = _manager_page(qt_app)

    page._use_boxes["Development"].setChecked(False)

    assert page._use_boxes["Development"].isChecked() is True
    assert settings.active_environment == "Development"


def test_ticking_a_row_use_checkbox_activates_that_environment(qt_app) -> None:
    settings, page = _manager_page(qt_app)
    activated: list[str] = []
    page.activated.connect(activated.append)

    page._use_boxes["QA"].setChecked(True)

    assert activated == ["QA"]


def test_row_delete_button_removes_that_row_not_the_selected_one(
    qt_app, no_dialogs
) -> None:
    settings, page = _manager_page(qt_app)
    page.table.selectRow(0)  # "Development" is selected...

    page._delete_buttons["QA"].click()  # ...but the QA row's button is clicked.

    assert "QA" not in settings.environments
    assert "Development" in settings.environments


def test_delete_is_disabled_when_only_one_environment_remains(qt_app) -> None:
    settings, page = _manager_page(qt_app, names=("Only",), active="Only")

    assert page._delete_buttons["Only"].isEnabled() is False


def test_row_clone_button_clones_that_row(qt_app, monkeypatch) -> None:
    from api_tester import environment_ui

    monkeypatch.setattr(
        environment_ui.QInputDialog, "getText", lambda *a, **k: ("QA Copy", True)
    )
    settings, page = _manager_page(qt_app)
    page.table.selectRow(0)  # "Development" selected, but QA's button is clicked.

    page._clone_buttons["QA"].click()

    assert "QA Copy" in settings.environments


def test_row_action_buttons_carry_theme_aware_icons(qt_app) -> None:
    settings, page = _manager_page(qt_app)

    for name in ("Development", "QA"):
        assert not page._clone_buttons[name].icon().isNull()
        assert not page._edit_buttons[name].icon().isNull()
        assert not page._delete_buttons[name].icon().isNull()
    assert not page.new_button.icon().isNull()
    # Re-tinting for a new palette must not drop any icon.
    page.refresh_theme()
    assert not page._delete_buttons["QA"].icon().isNull()



def test_disabling_a_service_binding_shows_disabled_status_and_disables_actions(
    widget,
) -> None:
    instance, manager, profile = widget
    identity = AuthProfile(name="Bearer", method="entra_pkce",
                            authority="https://login.example/tenant", client_id="abc")
    instance._profiles[identity.id] = identity
    instance._bindings["Reporting"].identity = identity.id
    instance._refresh_binding_choices()

    instance._disabled_boxes["Reporting"].setChecked(True)

    assert instance._bindings["Reporting"].disabled is True
    assert instance._status_labels["Reporting"].text() == "Disabled"
    assert not instance._sign_in_buttons["Reporting"].isEnabled()
    assert not instance._renew_buttons["Reporting"].isEnabled()


def test_profile_dialog_validation_error_is_shown_without_closing(qt_app) -> None:
    from api_tester.authentication_ui import AuthProfileDialog

    dialog = AuthProfileDialog(None, None)
    dialog.method_input.setCurrentIndex(dialog.method_input.findData("entra_pkce"))
    dialog.authority_input.setText("")  # missing required field
    dialog.client_id_input.setText("")

    dialog._accept()

    assert dialog.result_profile is None
    assert dialog.error_label.text()


def test_profile_dialog_only_offers_a_redirect_uri_for_interactive_methods(
    qt_app,
) -> None:
    from api_tester.authentication_ui import AuthProfileDialog

    dialog = AuthProfileDialog(None, None)

    dialog.method_input.setCurrentIndex(dialog.method_input.findData("entra_pkce"))
    assert dialog.redirect_uri_input.isVisibleTo(dialog)

    dialog.method_input.setCurrentIndex(dialog.method_input.findData("api_key"))
    assert not dialog.redirect_uri_input.isVisibleTo(dialog)


def test_profile_dialog_saves_a_pinned_redirect_uri(qt_app) -> None:
    from api_tester.authentication_ui import AuthProfileDialog

    dialog = AuthProfileDialog(None, None)
    dialog.method_input.setCurrentIndex(dialog.method_input.findData("entra_pkce"))
    dialog.authority_input.setText("https://login.microsoftonline.com/tenant")
    dialog.client_id_input.setText("client")
    dialog.scopes_input.setText("api://app/.default")
    dialog.redirect_uri_input.setText("  http://localhost:5000  ")

    dialog._accept()

    assert dialog.result_profile is not None
    assert dialog.result_profile.redirect_uri == "http://localhost:5000"
    assert dialog.result_profile.redirect_port() == 5000


def test_profile_dialog_reports_an_invalid_redirect_uri_without_closing(
    qt_app,
) -> None:
    from api_tester.authentication_ui import AuthProfileDialog

    dialog = AuthProfileDialog(None, None)
    dialog.method_input.setCurrentIndex(dialog.method_input.findData("entra_pkce"))
    dialog.authority_input.setText("https://login.microsoftonline.com/tenant")
    dialog.client_id_input.setText("client")
    dialog.scopes_input.setText("api://app/.default")
    dialog.redirect_uri_input.setText("http://127.0.0.1:5000")

    dialog._accept()

    assert dialog.result_profile is None
    assert "localhost" in dialog.error_label.text()


def test_profile_dialog_drops_the_redirect_uri_when_method_is_not_interactive(
    qt_app,
) -> None:
    from api_tester.authentication_ui import AuthProfileDialog

    dialog = AuthProfileDialog(None, None)
    dialog.method_input.setCurrentIndex(dialog.method_input.findData("entra_pkce"))
    dialog.redirect_uri_input.setText("http://localhost:5000")
    # Switching away must not leave a stale value that fails validation.
    dialog.method_input.setCurrentIndex(
        dialog.method_input.findData("client_credentials")
    )
    dialog.authority_input.setText("https://login.microsoftonline.com/tenant")
    dialog.client_id_input.setText("client")
    dialog.scopes_input.setText("api://app/.default")

    dialog._accept()

    assert dialog.result_profile is not None
    assert dialog.result_profile.redirect_uri == ""
