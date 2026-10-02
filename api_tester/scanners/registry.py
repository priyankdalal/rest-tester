"""Framework descriptors, folder detection, and scan dispatch."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from .base import IGNORED_DIRECTORIES, ScanResult


ScanCallable = Callable[[Path, str], ScanResult]


@dataclass(frozen=True)
class Framework:
    """One selectable project type in the catalog builder."""

    key: str
    label: str
    language: str
    #: Files whose presence strongly implies this framework.
    marker_files: tuple[str, ...] = ()
    #: Substrings looked for inside dependency manifests and source files.
    marker_text: tuple[str, ...] = ()
    #: Source suffixes sampled when looking for ``marker_text``.
    source_suffixes: tuple[str, ...] = ()
    description: str = ""
    _scan: ScanCallable | None = field(default=None, repr=False, compare=False)

    def scan(self, root: Path, service_name: str) -> ScanResult:
        if self._scan is None:  # pragma: no cover - defensive
            raise RuntimeError(f"Framework '{self.key}' has no scanner.")
        return self._scan(root, service_name)


def _dotnet(root: Path, service_name: str) -> ScanResult:
    from . import dotnet

    return dotnet.scan(root, service_name)


def _fastapi(root: Path, service_name: str) -> ScanResult:
    from . import python_web

    return python_web.scan_fastapi(root, service_name)


def _flask(root: Path, service_name: str) -> ScanResult:
    from . import python_web

    return python_web.scan_flask(root, service_name)


def _django(root: Path, service_name: str) -> ScanResult:
    from . import python_web

    return python_web.scan_django(root, service_name)


def _express(root: Path, service_name: str) -> ScanResult:
    from . import javascript

    return javascript.scan_express(root, service_name)


def _nestjs(root: Path, service_name: str) -> ScanResult:
    from . import javascript

    return javascript.scan_nestjs(root, service_name)


def _spring(root: Path, service_name: str) -> ScanResult:
    from . import java

    return java.scan_spring(root, service_name)


def _jaxrs(root: Path, service_name: str) -> ScanResult:
    from . import java

    return java.scan_jaxrs(root, service_name)


def _laravel(root: Path, service_name: str) -> ScanResult:
    from . import php

    return php.scan_laravel(root, service_name)


def _symfony(root: Path, service_name: str) -> ScanResult:
    from . import php

    return php.scan_symfony(root, service_name)


def _slim(root: Path, service_name: str) -> ScanResult:
    from . import php

    return php.scan_slim(root, service_name)


def _openapi(root: Path, service_name: str) -> ScanResult:
    from . import openapi

    return openapi.scan(root, service_name)


FRAMEWORKS: tuple[Framework, ...] = (
    Framework(
        key="dotnet",
        label="ASP.NET Core Web API (C#)",
        language="C#",
        marker_files=("*.sln", "*.csproj"),
        marker_text=("Microsoft.AspNetCore", "ControllerBase", "[ApiController]"),
        source_suffixes=(".cs", ".csproj"),
        description=(
            "Full Rest Tester-grade scan: controllers, generic base controllers, "
            "filter/payload/form schemas, and enums."
        ),
        _scan=_dotnet,
    ),
    Framework(
        key="fastapi",
        label="FastAPI (Python)",
        language="Python",
        marker_files=("main.py", "app.py"),
        marker_text=("from fastapi", "FastAPI(", "APIRouter("),
        source_suffixes=(".py", ".txt", ".toml"),
        description="Reads @app/@router decorators and typed handler signatures.",
        _scan=_fastapi,
    ),
    Framework(
        key="flask",
        label="Flask (Python)",
        language="Python",
        marker_files=("app.py", "wsgi.py"),
        marker_text=("from flask", "Flask(__name__)", "Blueprint("),
        source_suffixes=(".py", ".txt", ".toml"),
        description="Reads @app.route/@blueprint.route decorators and url_prefix.",
        _scan=_flask,
    ),
    Framework(
        key="django",
        label="Django / Django REST Framework (Python)",
        language="Python",
        marker_files=("manage.py", "urls.py"),
        marker_text=("django.urls", "rest_framework", "DJANGO_SETTINGS_MODULE"),
        source_suffixes=(".py", ".txt", ".toml"),
        description="Reads urls.py path()/re_path() plus DRF router registrations.",
        _scan=_django,
    ),
    Framework(
        key="express",
        label="Express / Koa (JavaScript, TypeScript)",
        language="JavaScript",
        marker_files=("package.json",),
        marker_text=('"express"', "require('express')", 'from "express"', '"koa"'),
        source_suffixes=(".js", ".ts", ".json"),
        description="Reads app/router verb registrations and .route() chains.",
        _scan=_express,
    ),
    Framework(
        key="nestjs",
        label="NestJS (TypeScript)",
        language="TypeScript",
        marker_files=("nest-cli.json", "package.json"),
        marker_text=("@nestjs/common", "@Controller("),
        source_suffixes=(".ts", ".json"),
        description="Reads @Controller classes with @Get/@Post decorators.",
        _scan=_nestjs,
    ),
    Framework(
        key="spring",
        label="Spring Boot (Java, Kotlin)",
        language="Java",
        marker_files=("pom.xml", "build.gradle", "build.gradle.kts"),
        marker_text=("org.springframework", "@RestController", "@RequestMapping"),
        source_suffixes=(".java", ".kt", ".xml", ".gradle"),
        description="Reads @RestController classes and mapping annotations.",
        _scan=_spring,
    ),
    Framework(
        key="jaxrs",
        label="JAX-RS / Quarkus / Jersey (Java)",
        language="Java",
        marker_files=("pom.xml", "build.gradle"),
        marker_text=("javax.ws.rs", "jakarta.ws.rs", "io.quarkus"),
        source_suffixes=(".java", ".kt", ".xml"),
        description="Reads @Path resources with @GET/@POST method annotations.",
        _scan=_jaxrs,
    ),
    Framework(
        key="laravel",
        label="Laravel / Lumen (PHP)",
        language="PHP",
        marker_files=("artisan", "composer.json"),
        marker_text=("laravel/framework", "Illuminate\\", "Route::"),
        source_suffixes=(".php", ".json"),
        description="Reads routes/*.php verb calls, groups, and apiResource.",
        _scan=_laravel,
    ),
    Framework(
        key="symfony",
        label="Symfony (PHP)",
        language="PHP",
        marker_files=("composer.json", "symfony.lock"),
        marker_text=("symfony/framework-bundle", "#[Route(", "@Route("),
        source_suffixes=(".php", ".json"),
        description="Reads #[Route] attributes and @Route annotations.",
        _scan=_symfony,
    ),
    Framework(
        key="slim",
        label="Slim / micro PHP router",
        language="PHP",
        marker_files=("composer.json",),
        marker_text=("slim/slim", "$app->get(", "$app->map("),
        source_suffixes=(".php", ".json"),
        description="Reads $app->verb() registrations and ->map() calls.",
        _scan=_slim,
    ),
    Framework(
        key="openapi",
        label="OpenAPI / Swagger document",
        language="Any",
        marker_files=(
            "openapi.json",
            "openapi.yaml",
            "openapi.yml",
            "swagger.json",
            "swagger.yaml",
            "swagger.yml",
        ),
        marker_text=('"openapi"', '"swagger"'),
        source_suffixes=(".json", ".yaml", ".yml"),
        description=(
            "Exact import from a published contract. Preferred whenever the "
            "service ships an OpenAPI document."
        ),
        _scan=_openapi,
    ),
)

_BY_KEY = {framework.key: framework for framework in FRAMEWORKS}

#: Files sampled when guessing a framework; keeps detection fast on big trees.
_MAX_SAMPLED_FILES = 400


def framework_keys() -> list[str]:
    return [framework.key for framework in FRAMEWORKS]


def framework_by_key(key: str) -> Framework:
    try:
        return _BY_KEY[key]
    except KeyError:
        raise KeyError(
            f"Unknown framework '{key}'. Known types: {', '.join(framework_keys())}."
        ) from None


def _iter_candidate_files(root: Path, suffixes: set[str]) -> list[Path]:
    collected: list[Path] = []
    for path in root.rglob("*"):
        if len(collected) >= _MAX_SAMPLED_FILES:
            break
        if path.is_dir() or any(part in IGNORED_DIRECTORIES for part in path.parts):
            continue
        if path.suffix.lower() in suffixes:
            collected.append(path)
    return collected


def detect_frameworks(root: Path) -> list[tuple[Framework, int]]:
    """Scores each framework against ``root``, best match first.

    The result is a *suggestion*: the builder still asks the user to confirm
    the project type before scanning.
    """
    root = Path(root)
    if not root.exists():
        return []

    all_suffixes: set[str] = set()
    for framework in FRAMEWORKS:
        all_suffixes.update(suffix.lower() for suffix in framework.source_suffixes)
    sampled = _iter_candidate_files(root, all_suffixes)
    contents: dict[Path, str] = {}
    for path in sampled:
        try:
            if path.stat().st_size > 512_000:
                continue
            contents[path] = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue

    scored: list[tuple[Framework, int]] = []
    for framework in FRAMEWORKS:
        score = 0
        for pattern in framework.marker_files:
            if any(True for _ in root.glob(pattern)):
                score += 3
            elif any(True for _ in root.glob(f"*/{pattern}")):
                score += 2
        suffixes = {suffix.lower() for suffix in framework.source_suffixes}
        for path, text in contents.items():
            if path.suffix.lower() not in suffixes:
                continue
            if any(marker in text for marker in framework.marker_text):
                score += 2
                if score >= 12:
                    break
        if score:
            scored.append((framework, score))

    scored.sort(key=lambda item: (-item[1], item[0].label))
    return scored


def scan_project(root: Path | str, service_name: str, framework_key: str) -> ScanResult:
    """Scans ``root`` as ``framework_key`` and returns the discovered endpoints."""
    path = Path(root)
    if not path.exists():
        raise FileNotFoundError(f"Folder does not exist: {path}")
    framework = framework_by_key(framework_key)
    result = framework.scan(path, service_name)
    result.framework = framework.key
    return result
