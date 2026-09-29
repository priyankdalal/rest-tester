"""The profile dialog must render every method from its declared fields.

Adding an authentication method should require no dialog code, so these
assert the generated rows rather than any hand-written layout.
"""

from __future__ import annotations

import pytest
from PyQt6.QtWidgets import QCheckBox, QComboBox, QLineEdit, QSpinBox

from tests.test_authentication_ui import qt_app  # noqa: F401 - Qt application fixture

from api_tester import authentication_ui
from api_tester.auth_strategies import STRATEGY_IDS, get_strategy
from api_tester.authentication import AuthProfile


@pytest.fixture(name="app")
def _app(qt_app):  # noqa: F811 - the imported fixture is the dependency
    return qt_app


def dialog_for(method: str) -> authentication_ui.AuthProfileDialog:
    dialog = authentication_ui.AuthProfileDialog(AuthProfile(method=method))
    index = dialog.method_input.findData(method)
    dialog.method_input.setCurrentIndex(index)
    dialog._sync_fields()
    return dialog


@pytest.mark.parametrize("method", sorted(STRATEGY_IDS))
def test_every_method_is_selectable_and_renders_all_its_fields(app, method) -> None:  # noqa: F811
    dialog = dialog_for(method)
    assert dialog.method_input.currentData() == method

    strategy = get_strategy(method)
    rendered = set(dialog._dynamic_inputs) | dialog._FIXED_CONFIG
    for field in strategy.config_fields:
        assert field.name in rendered, f"{method} config field {field.name} has no row"

    if len(strategy.secret_fields) > 1:
        assert set(dialog._dynamic_secret_inputs) == {
            field.name for field in strategy.secret_fields
        }


@pytest.mark.parametrize("method", sorted(STRATEGY_IDS))
def test_no_method_leaves_a_stale_row_from_the_previous_selection(app, method) -> None:  # noqa: F811
    """Switching methods must not carry another method's inputs over."""
    dialog = dialog_for("aws_sigv4")
    index = dialog.method_input.findData(method)
    dialog.method_input.setCurrentIndex(index)
    dialog._sync_fields()

    declared = {field.name for field in get_strategy(method).config_fields}
    assert set(dialog._dynamic_inputs) <= declared


@pytest.mark.parametrize("method", sorted(STRATEGY_IDS))
def test_a_hidden_row_never_leaves_its_label_behind(app, method) -> None:
    """A label must not outlive the input it belongs to.

    The fixed rows live in a QFormLayout nested in an outer QVBoxLayout, so
    a naive `parentWidget().layout()` lookup silently failed and left labels
    such as "Scopes" on screen for methods that have no scopes.
    """
    dialog = dialog_for(method)
    for name, widget in (
        ("authority", dialog.authority_input),
        ("client_id", dialog.client_id_input),
        ("scopes", dialog.scopes_input),
        ("header_name", dialog.header_name_input),
        ("redirect_uri", dialog.redirect_uri_input),
        ("login_timeout", dialog.timeout_input),
    ):
        label = authentication_ui._row_label(widget)
        assert label is not None, f"{method}: no label found for {name}"
        assert label.isVisibleTo(dialog) == widget.isVisibleTo(dialog), (
            f"{method}: {name} label visibility does not match its input"
        )


@pytest.mark.parametrize("method", sorted(STRATEGY_IDS))
def test_fixed_rows_are_worded_by_the_selected_method(app, method) -> None:
    dialog = dialog_for(method)
    declared = {field.name: field for field in get_strategy(method).config_fields}
    for name, widget in (
        ("authority", dialog.authority_input),
        ("scopes", dialog.scopes_input),
        ("header_name", dialog.header_name_input),
    ):
        field = declared.get(name)
        if field is None:
            continue
        assert authentication_ui._row_label(widget).text() == field.label


def test_an_api_key_row_is_not_labelled_header_name(app) -> None:
    """The key can be placed in the query string, so "Header name" is wrong."""
    dialog = dialog_for("api_key")
    assert authentication_ui._row_label(dialog.header_name_input).text() == (
        "Parameter name"
    )


def test_a_generic_provider_does_not_show_microsoft_placeholders(app) -> None:
    dialog = dialog_for("oauth2")
    assert "microsoftonline" not in dialog.authority_input.placeholderText()
    assert dialog.scopes_input.placeholderText() == "openid profile"


def test_switching_methods_rewords_a_shared_row(app) -> None:
    dialog = dialog_for("api_key")
    assert authentication_ui._row_label(dialog.header_name_input).text() == (
        "Parameter name"
    )
    scopes_before = dialog.scopes_input.placeholderText()

    index = dialog.method_input.findData("entra_pkce")
    dialog.method_input.setCurrentIndex(index)
    dialog._sync_fields()

    assert dialog.scopes_input.placeholderText() != scopes_before
    assert authentication_ui._row_label(dialog.scopes_input).text() == (
        "Scopes (comma separated)"
    )


def test_the_redirect_uri_keeps_its_guidance_tooltip(app) -> None:
    for method in ("entra_pkce", "oauth2"):
        dialog = dialog_for(method)
        assert dialog.redirect_uri_input.toolTip()


def test_config_field_kinds_map_to_the_matching_widget(app) -> None:  # noqa: F811
    dialog = dialog_for("aws_sigv4")
    assert isinstance(dialog._dynamic_inputs["region"], QLineEdit)
    assert isinstance(dialog._dynamic_inputs["sign_payload_header"], QCheckBox)

    oauth = dialog_for("oauth2")
    assert isinstance(oauth._dynamic_inputs["grant_type"], QComboBox)
    assert isinstance(oauth._dynamic_inputs["token_url"], QLineEdit)

    jwt = dialog_for("jwt_bearer")
    assert isinstance(jwt._dynamic_inputs["lifetime"], QSpinBox)


def test_existing_option_values_are_loaded_into_the_rows(app) -> None:  # noqa: F811
    profile = AuthProfile(
        method="aws_sigv4",
        options={"region": "eu-west-1", "service": "s3", "sign_payload_header": True},
    )
    dialog = authentication_ui.AuthProfileDialog(profile)

    assert dialog._dynamic_inputs["region"].text() == "eu-west-1"
    assert dialog._dynamic_inputs["service"].text() == "s3"
    assert dialog._dynamic_inputs["sign_payload_header"].isChecked()


def test_accepting_collects_options_and_every_secret(app, monkeypatch) -> None:  # noqa: F811
    dialog = dialog_for("aws_sigv4")
    dialog.name_input.setText("AWS")
    dialog._dynamic_inputs["region"].setText("us-east-1")
    dialog._dynamic_inputs["service"].setText("execute-api")
    dialog._dynamic_secret_inputs["access_key"].setText("AKIDEXAMPLE")
    dialog._dynamic_secret_inputs["secret_key"].setText("s" * 40)
    dialog._accept()

    assert dialog.result_profile is not None
    assert dialog.result_profile.options["region"] == "us-east-1"
    assert dialog.secret_values == {
        "access_key": "AKIDEXAMPLE", "secret_key": "s" * 40
    }


def test_a_single_secret_method_still_uses_the_dedicated_row(app) -> None:  # noqa: F811
    dialog = dialog_for("manual_bearer")
    assert dialog._dynamic_secret_inputs == {}
    dialog.name_input.setText("Token")
    dialog.secret_input.setText("the-token")
    dialog._accept()

    assert dialog.secret_values == {"token": "the-token"}


def test_secret_rows_never_echo_their_contents(app) -> None:  # noqa: F811
    for method in sorted(STRATEGY_IDS):
        dialog = dialog_for(method)
        for name, editor in dialog._dynamic_secret_inputs.items():
            assert editor.echoMode() == QLineEdit.EchoMode.Password, (
                f"{method}.{name} is not masked"
            )
    assert dialog_for("manual_bearer").secret_input.echoMode() == (
        QLineEdit.EchoMode.Password
    )


def test_invalid_configuration_is_reported_instead_of_accepted(app) -> None:  # noqa: F811
    dialog = dialog_for("oauth2")
    dialog.name_input.setText("Broken")
    dialog._dynamic_inputs["token_url"].setText("")
    dialog._accept()

    assert dialog.result_profile is None
    assert dialog.error_label.text()


def test_a_non_https_token_url_is_rejected(app) -> None:  # noqa: F811
    dialog = dialog_for("oauth2")
    dialog.name_input.setText("Insecure")
    dialog.client_id_input.setText("app-1")
    dialog._dynamic_inputs["token_url"].setText("http://id.example.test/token")
    dialog._dynamic_inputs["grant_type"].setCurrentIndex(
        dialog._dynamic_inputs["grant_type"].findData("client_credentials")
    )
    dialog._accept()

    assert dialog.result_profile is None
    assert "HTTPS" in dialog.error_label.text()
