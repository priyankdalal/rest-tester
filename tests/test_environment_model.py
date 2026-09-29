from api_tester.environment import AppSettings, validate_base_url


DEFAULTS = {"Reporting": "https://localhost:7262", "TrialAuth": ""}


def test_legacy_settings_migrate_to_development_profile() -> None:
    settings = AppSettings.from_dict(
        {
            "base_urls": {"Reporting": "http://localhost:5262"},
            "verify_ssl": False,
            "request_timeout": 180,
        },
        DEFAULTS,
    )
    assert settings.active_environment == "Development"
    assert settings.active.base_urls["Reporting"] == "http://localhost:5262"
    assert settings.active.verify_ssl is False
    assert settings.active.request_timeout == 180


def test_named_environments_keep_one_url_per_service() -> None:
    settings = AppSettings.from_dict(
        {
            "active_environment": "QA",
            "environments": {
                "Development": {"base_urls": {"Reporting": "http://localhost:5262"}},
                "QA": {"base_urls": {"Reporting": "https://qa.example/reporting"}},
            },
        },
        DEFAULTS,
    )
    assert settings.active.base_urls["Reporting"] == "https://qa.example/reporting"
    assert "TrialAuth" in settings.active.base_urls


def test_settings_round_trip_preserves_shell_state_without_secrets() -> None:
    settings = AppSettings.from_dict({}, DEFAULTS)
    settings.favorites = ["reporting.get"]
    settings.recent_endpoints = ["reporting.get"]
    document = settings.to_dict()
    assert "access_token" not in document
    assert "api_key" not in document
    restored = AppSettings.from_dict(document, DEFAULTS)
    assert restored.favorites == ["reporting.get"]
    assert restored.recent_endpoints == ["reporting.get"]


def test_environment_variables_and_custom_headers_round_trip() -> None:
    settings = AppSettings.from_dict(
        {
            "environments": {
                "Development": {
                    "base_urls": DEFAULTS,
                    "variables": {"trialId": "470"},
                    "custom_headers": {"X-Tenant": "local"},
                }
            }
        },
        DEFAULTS,
    )
    restored = AppSettings.from_dict(settings.to_dict(), DEFAULTS)
    assert restored.active.variables == {"trialId": "470"}
    assert restored.active.custom_headers == {"X-Tenant": "local"}


def test_url_validation_is_shape_only() -> None:
    assert validate_base_url("https://localhost:7262") == (True, "Configured")
    assert validate_base_url("http://localhost:5262") == (True, "Configured")
    assert validate_base_url("localhost:5262") == (False, "Invalid URL")
    assert validate_base_url("") == (False, "No base URL")
