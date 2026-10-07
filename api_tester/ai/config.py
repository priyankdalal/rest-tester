"""Provider settings in JSON; hosted API keys in OS-encrypted storage."""

from __future__ import annotations

import json
import logging
import os
import re
import tempfile
import uuid
from dataclasses import asdict, dataclass, field as dataclass_field, fields, replace
from pathlib import Path
from typing import Any

from .provider import LlmProvider

PROVIDERS = ("ollama", "azure", "openai", "claude", "sarvam")
PROVIDER_LABELS = {
    "ollama": "Ollama", "azure": "Azure OpenAI", "openai": "OpenAI", "claude": "Claude (Anthropic)",
    "sarvam": "Sarvam AI",
}
PROVIDER_DEFAULTS = {
    "ollama": {"endpoint": "http://localhost:11434", "planner_model": "qwen3.6:latest"},
    "azure": {"endpoint": "", "planner_model": "", "api_version": "2024-10-21"},
    "openai": {"endpoint": "https://api.openai.com/v1", "planner_model": "gpt-4.1-mini"},
    "claude": {"endpoint": "https://api.anthropic.com", "planner_model": "claude-sonnet-4-6"},
    "sarvam": {"endpoint": "https://api.sarvam.ai/v1", "planner_model": "sarvam-105b"},
}
KEY_ENVIRONMENTS = {
    "azure": "AZURE_OPENAI_API_KEY", "openai": "OPENAI_API_KEY", "claude": "ANTHROPIC_API_KEY",
    "sarvam": "SARVAM_API_KEY",
}
KEY_DIRECTORY = Path(__file__).resolve().parents[2] / "data" / "ai_credentials"
DEFAULT_MODEL = "qwen3.6:latest"


@dataclass
class AiConnection:
    id: str = dataclass_field(default_factory=lambda: uuid.uuid4().hex)
    name: str = "Local Ollama"
    provider: str = "ollama"
    endpoint: str = "http://localhost:11434"
    planner_model: str = DEFAULT_MODEL
    think: bool = False
    context_window: int = 16384
    api_version: str = "2024-10-21"
    allow_hosted: bool = False
    credential_id: str = ""
    key_environment: str = ""

    @classmethod
    def create(cls, name: str, provider: str = "ollama") -> "AiConnection":
        if provider not in PROVIDERS:
            raise ValueError(f"Unknown AI provider: {provider}")
        connection = cls(name=name, provider=provider, **PROVIDER_DEFAULTS[provider])
        connection.credential_id = connection.id
        return connection

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "AiConnection":
        if not isinstance(value, dict):
            raise ValueError("An AI connection must be an object.")
        known = {item.name for item in fields(cls)}
        connection = cls(**{key: item for key, item in value.items() if key in known})
        if connection.provider not in PROVIDERS:
            raise ValueError("Unknown provider in AI connection.")
        if not isinstance(connection.name, str) or not connection.name.strip():
            raise ValueError("An AI connection needs a name.")
        _credential_path(connection.id)
        if not connection.credential_id:
            connection.credential_id = connection.id
        _credential_path(connection.credential_id)
        connection.context_window = max(2048, min(int(connection.context_window), 262144))
        return connection


@dataclass
class AiSettings:
    provider: str = "ollama"
    endpoint: str = "http://localhost:11434"
    planner_model: str = DEFAULT_MODEL
    #: Reasoning ("thinking") mode is slower but can help hard prompts.
    think: bool = False
    timeout_seconds: int = 600
    max_repairs: int = 2
    candidate_count: int = 8
    detailed_candidates: int = 3
    #: Ollama's default 4096-token window silently truncates suite prompts.
    context_window: int = 16384
    api_version: str = "2024-10-21"
    provider_configs: dict[str, dict[str, Any]] = dataclass_field(default_factory=dict)
    allow_hosted: bool = False
    connections: list[AiConnection] = dataclass_field(default_factory=list)
    active_connection_id: str = ""
    credential_id: str = ""
    key_environment: str = ""
    connection_name: str = ""

    @classmethod
    def from_dict(cls, value: dict[str, Any] | None) -> "AiSettings":
        if value is None:
            value = {}
        if not isinstance(value, dict):
            raise ValueError("AI settings must be an object.")
        known = {item.name for item in fields(cls)}
        settings = cls(**{key: item for key, item in value.items() if key in known})
        if settings.provider not in PROVIDERS:
            settings.provider = "ollama"
        if not isinstance(settings.provider_configs, dict):
            raise ValueError("AI provider profiles must be an object.")
        profile_fields = {"endpoint", "planner_model", "think", "context_window", "api_version"}
        settings.provider_configs = {
            provider: {key: item for key, item in profile.items() if key in profile_fields}
            for provider, profile in settings.provider_configs.items()
            if provider in PROVIDERS and isinstance(profile, dict)
        }
        settings.timeout_seconds = max(10, min(int(settings.timeout_seconds), 1800))
        settings.max_repairs = max(0, min(int(settings.max_repairs), 50))
        settings.candidate_count = max(1, min(int(settings.candidate_count), 20))
        settings.detailed_candidates = max(1, min(int(settings.detailed_candidates), settings.candidate_count))
        settings.context_window = max(2048, min(int(settings.context_window), 262144))
        if not isinstance(settings.connections, list):
            raise ValueError("AI connections must be a list.")
        settings.connections = [AiConnection.from_dict(item) for item in settings.connections]
        if settings.connections:
            ids = [connection.id for connection in settings.connections]
            names = [connection.name.strip().casefold() for connection in settings.connections]
            if len(set(ids)) != len(ids) or len(set(names)) != len(names):
                raise ValueError("AI connection IDs and names must be unique.")
            if settings.active_connection_id not in ids:
                raise ValueError("The active AI connection does not exist.")
            settings = settings.for_connection(settings.active_connection_id)
        else:
            settings.ensure_connections()
        return settings

    def to_dict(self) -> dict[str, Any]:
        settings = self.for_connection(self.active_connection_id) if self.connections else self
        return asdict(settings)

    def ensure_connections(self) -> None:
        """Migrate the single-provider format without changing its active choice or key."""
        if self.connections:
            return
        for provider in dict.fromkeys((self.provider, *self.provider_configs)):
            profile = self.for_provider(provider)
            connection = AiConnection.create(PROVIDER_LABELS[provider], provider)
            connection.id = uuid.uuid5(uuid.NAMESPACE_URL, f"rest-tester:legacy-ai:{provider}").hex
            connection.endpoint = profile.endpoint
            connection.planner_model = profile.planner_model
            connection.think = profile.think
            connection.context_window = profile.context_window
            connection.api_version = profile.api_version
            connection.allow_hosted = self.allow_hosted
            connection.credential_id = provider
            connection.key_environment = KEY_ENVIRONMENTS.get(provider, "")
            self.connections.append(connection)
            if provider == self.provider:
                self.active_connection_id = connection.id
        self.connection_name = self.connections[0].name
        self.credential_id = self.provider
        self.key_environment = KEY_ENVIRONMENTS.get(self.provider, "")

    def for_connection(self, connection_id: str) -> "AiSettings":
        connection = next((item for item in self.connections if item.id == connection_id), None)
        if connection is None:
            raise ValueError("The selected AI connection does not exist.")
        profile = asdict(connection)
        profile.pop("id")
        name = profile.pop("name")
        return replace(self, **profile, active_connection_id=connection_id, connection_name=name)

    def for_provider(self, provider: str) -> "AiSettings":
        if provider not in PROVIDERS:
            raise ValueError(f"Unknown AI provider: {provider}")
        profile = dict(PROVIDER_DEFAULTS[provider])
        profile.update(self.provider_configs.get(provider, {}))
        if provider == self.provider:
            profile.update(endpoint=self.endpoint, planner_model=self.planner_model,
                           think=self.think, context_window=self.context_window, api_version=self.api_version)
        return replace(self, provider=provider, **profile)


def load_ai_settings(path: Path, *, strict: bool = False) -> AiSettings:
    try:
        with path.open(encoding="utf-8") as stream:
            return AiSettings.from_dict(json.load(stream))
    except FileNotFoundError:
        if strict:
            raise
        return AiSettings()
    except (OSError, ValueError, TypeError) as exc:
        logging.getLogger(__name__).warning("Unable to load AI settings from %s (%s)%s",
                                          path, type(exc).__name__, "." if strict else "; using defaults.")
        if strict:
            raise
        return AiSettings()


def save_ai_settings(path: Path, settings: AiSettings) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent, delete=False) as stream:
            temporary = Path(stream.name)
            json.dump(settings.to_dict(), stream, indent=2)
        os.replace(temporary, path)
    finally:
        if temporary is not None and temporary.exists():
            temporary.unlink()


def build_provider(settings: AiSettings, *, api_key: str = "") -> LlmProvider:
    from .providers.ollama import OllamaProvider

    if settings.provider == "ollama":
        return OllamaProvider(settings.endpoint, think=settings.think, context_window=settings.context_window)
    from .provider import LlmAuthError
    from .providers.hosted import HostedProvider

    if not settings.allow_hosted:
        raise LlmAuthError("Enable hosted AI in Settings before sending prompts to a cloud provider.")
    return HostedProvider(settings.provider, settings.endpoint,
                          api_key or get_ai_key(settings.provider, settings.credential_id, settings.key_environment),
                          api_version=settings.api_version)


def _credential_path(credential_id: str) -> Path:
    if not isinstance(credential_id, str) or not re.fullmatch(r"[a-zA-Z0-9_-]{1,80}", credential_id):
        raise ValueError("Invalid AI credential identifier.")
    return KEY_DIRECTORY / f"{credential_id}.secret"


def get_ai_key(provider: str, credential_id: str = "", key_environment: str = "") -> str:
    from .provider import LlmAuthError

    environment = key_environment or (KEY_ENVIRONMENTS.get(provider, "") if not credential_id else "")
    value = os.environ.get(environment, "").strip() if environment else ""
    if value:
        return value
    path = _credential_path(credential_id or provider)
    if not path.exists():
        hint = f" or set {environment}" if environment else ""
        raise LlmAuthError(f"Add an API key for this AI connection{hint}.")
    from msal_extensions import build_encrypted_persistence

    try:
        return build_encrypted_persistence(str(path)).load()
    except (OSError, RuntimeError, ValueError) as exc:
        raise LlmAuthError("Cannot read the encrypted AI key. Re-enter it in Settings.") from exc


def save_ai_key(provider: str, value: str, credential_id: str = "") -> None:
    from msal_extensions import build_encrypted_persistence

    if provider not in KEY_ENVIRONMENTS:
        raise ValueError("This provider does not use an API key.")
    KEY_DIRECTORY.mkdir(parents=True, exist_ok=True)
    build_encrypted_persistence(str(_credential_path(credential_id or provider))).save(value.strip())


def delete_ai_key(credential_id: str) -> None:
    _credential_path(credential_id).unlink(missing_ok=True)
