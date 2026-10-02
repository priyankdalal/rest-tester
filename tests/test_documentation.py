import json
from pathlib import Path

from api_tester.catalog import Endpoint, Service
from api_tester.documentation import endpoint_documentation, openapi_operation, xml_summary


def endpoint() -> Endpoint:
    return Endpoint(
        id="reporting.export",
        service="Reporting",
        controller="OnFarmHarvestData",
        action="Export",
        method="POST",
        path="/OnFarmHarvestData/{id}/export",
        source_file="Controller.cs",
        source_line=8,
    )


def test_reads_summary_immediately_above_action(tmp_path: Path) -> None:
    source = tmp_path / "Controller.cs"
    source.write_text(
        "\n\n/// <summary>\n/// Exports harvest data.\n/// </summary>\n[HttpPost]\npublic void Export() {}\n",
        encoding="utf-8",
    )
    assert xml_summary(source, 6) == "Exports harvest data."


def test_openapi_operation_is_optional(tmp_path: Path) -> None:
    assert openapi_operation(tmp_path, endpoint()) is None


def test_documentation_prefers_openapi_summary(tmp_path: Path) -> None:
    repository = tmp_path / "Reporting"
    repository.mkdir()
    (repository / "Controller.cs").write_text("", encoding="utf-8")
    openapi = tmp_path / "openapi"
    openapi.mkdir()
    (openapi / "Reporting.json").write_text(
        json.dumps(
            {
                "paths": {
                    "/OnFarmHarvestData/{id}/export": {
                        "post": {
                            "summary": "OpenAPI export summary",
                            "responses": {"200": {"description": "Success"}},
                        }
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    service = Service("Reporting", "Reporting", "", (endpoint(),))
    text = endpoint_documentation(endpoint(), service, tmp_path, openapi)
    assert text.startswith("OpenAPI export summary")
    assert "200: Success" in text
