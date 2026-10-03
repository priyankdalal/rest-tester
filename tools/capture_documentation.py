"""Capture current UI with synthetic plans and isolated data, never live APIs.

Run from the repository root: python -m tools.capture_documentation
"""

from contextlib import ExitStack
import json
from pathlib import Path
import tempfile
import time
from unittest.mock import patch

from PyQt6.QtGui import QFont, QFontDatabase
from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import QApplication, QLabel

from api_tester import theme
from api_tester.ai.config import AiConnection, AiSettings, PROVIDERS, save_ai_settings
from api_tester.ai.provider import LlmResponse
from api_tester.ai.providers.fake import FakeProvider
from api_tester.ai.request_plan import RequestPlan
from api_tester.ai.suite_plan import SuiteCasePlan, SuitePlan
from api_tester.ai.usage_store import UsageStore
from api_tester.ai.workflow_plan import WorkflowPlan
from api_tester.builders import QueryBuilder, SortBuilder
from api_tester.catalog import load_catalog
from api_tester.data_runner.csv_source import CsvImportSettings
from api_tester.data_runner.mapping import ColumnMapping
from api_tester.load_testing.scenario import LoadStage, ThresholdDefinition

ROOT = Path(__file__).resolve().parents[1]
IMAGES = ROOT / "docs" / "images"
PRODUCT_LIST = "f76aeb49ca37"
PRODUCT_BY_ID = "870dcc89426b"


def settle(app: QApplication) -> None:
    for _ in range(12):
        app.processEvents()


def capture(app: QApplication, widget, name: str) -> None:
    widget.show()
    settle(app)
    if not widget.grab().save(str(IMAGES / f"{name}.png")):
        raise OSError(f"Could not save screenshot: {name}")
    print(f"Captured {name}")


def main() -> None:
    from api_tester import main as main_module
    from api_tester.ai import usage_store
    from api_tester.ai.ui import AskAiDialog
    from api_tester.settings_ui import SettingsDialog

    app = QApplication.instance() or QApplication([])
    windows_font = Path(r"C:\Windows\Fonts\segoeui.ttf")
    if windows_font.exists():
        QFontDatabase.addApplicationFont(str(windows_font))
        app.setFont(QFont("Segoe UI", 10))
    theme.apply_theme(app, "Light")
    IMAGES.mkdir(parents=True, exist_ok=True)
    catalog = load_catalog(ROOT / "examples" / "store_catalog.json")
    with tempfile.TemporaryDirectory(prefix="rest-tester-docs-") as folder, ExitStack() as stack:
        temp = Path(folder)
        settings_path = temp / "ai.json"
        connections = [AiConnection.create("Demo " + provider, provider) for provider in PROVIDERS]
        settings = AiSettings(connections=connections, active_connection_id=connections[0].id)
        save_ai_settings(settings_path, settings)
        store = UsageStore(temp / "usage.db")
        for name, value in (
            ("SETTINGS_PATH", temp / "settings.json"),
            ("WORKSPACE_DB_PATH", temp / "workspace.db"),
            ("AI_SETTINGS_PATH", settings_path),
            ("CATALOG_PATH", ROOT / "examples" / "store_catalog.json"),
        ):
            stack.enter_context(patch.object(main_module, name, value))
        stack.enter_context(patch.object(main_module.MainWindow, "_check_ai_connection", lambda _self: None))
        stack.enter_context(patch.object(usage_store, "UsageStore", lambda: store))
        window = main_module.MainWindow()
        stack.callback(window.workspace_store.close)
        stack.callback(window.close)
        window.resize(1440, 1000)
        assert window._load_catalog_from(ROOT / "examples" / "store_catalog.json")
        capture(app, window, "api-explorer-disabled")
        assert window._activate_endpoint(PRODUCT_LIST)
        window.ask_ai_button.set_available(True, "Offline documentation example")
        capture(app, window, "api-explorer-request")
        capture(app, window, "api-explorer")
        preview = window.grab().scaled(
            1200, 630, Qt.AspectRatioMode.KeepAspectRatioByExpanding,
            Qt.TransformationMode.SmoothTransformation,
        )
        preview = preview.copy((preview.width() - 1200) // 2, 0, 1200, 630)
        if not preview.save(str(ROOT / "docs" / "assets" / "og-image.png")):
            raise OSError("Could not save social preview")
        window._theme_changed("Dark")
        capture(app, window, "api-explorer-dark")
        window._theme_changed("Light")

        schema = catalog.filter_schema(window.current_endpoint.filter_entity)
        for builder_type, name in ((QueryBuilder, "query-builder"), (SortBuilder, "sort-builder")):
            builder = builder_type(schema)
            builder.resize(1160, 540)
            if isinstance(builder, QueryBuilder):
                builder.set_filter_string("Name__ct:=lamp;Status__eq:=Active")
            else:
                builder.set_sort_string("Price-,Name")
            capture(app, builder, name)
            builder.close()

        csv_path = temp / "products.csv"
        csv_path.write_text("ProductId\n42\n", encoding="utf-8")
        plans = [
            ("request", "List products, newest first",
             RequestPlan(endpoint_id=PRODUCT_LIST, title="Browse products", summary="Review before sending.")),
            ("suite", "Create a read-only regression suite for products",
             SuitePlan(name="Product read checks", cases=(
                 SuiteCasePlan(key="list", name="List products", endpoint_id=PRODUCT_LIST),
                 SuiteCasePlan(key="read", name="Read product 42", endpoint_id=PRODUCT_BY_ID,
                               parameters=(("id", "42"),)),
             ))),
            ("data", "Get each product using ProductId from the CSV",
             WorkflowPlan(mode="data", endpoint_id=PRODUCT_BY_ID, title="Products from CSV",
                          mappings=(ColumnMapping("ProductId", "path:id"),))),
            ("load", "Load test the products list with 5 virtual users",
             WorkflowPlan(mode="load", endpoint_id=PRODUCT_LIST, title="Product list load profile",
                          stages=(LoadStage("ramp_up", 30, 1, 5), LoadStage("steady", 60, 5, 5),
                                  LoadStage("ramp_down", 15, 5, 0)),
                          thresholds=(ThresholdDefinition("p95_ms", "<=", 500),))),
        ]
        for mode, prompt, plan in plans:
            value = plan.to_json()
            provider = FakeProvider([LlmResponse(
                text=json.dumps(value), json=value, input_tokens=1200, output_tokens=300,
                model=settings.planner_model, extra={"finish_reason": "stop"},
            )])
            with patch("api_tester.ai.controller.build_provider", return_value=provider):
                dialog = AskAiDialog(
                    window, catalog=lambda: catalog, secrets=lambda: (), on_open=lambda _request: None,
                    settings_path=settings_path, on_open_suite=lambda _suite: None,
                    on_open_data=lambda *_args: None, on_open_load=lambda *_args: None,
                    on_settings=lambda: None,
                )
                dialog.resize(1000, 900)
                dialog.set_mode(mode)
                if mode == "data":
                    dialog.csv_settings = CsvImportSettings(path=str(csv_path))
                    dialog.csv_columns = ("ProductId",)
                    dialog.csv_button.setText("CSV: products.csv")
                dialog.prompt_input.setPlainText(prompt)
                dialog.show()
                dialog.ask()
                deadline = time.monotonic() + 15
                while dialog._task is not None and time.monotonic() < deadline:
                    app.processEvents()
                    time.sleep(0.01)
                if dialog._task is not None:
                    raise TimeoutError(f"Documentation plan did not finish: {mode}")
                outcome = dialog.suite_outcome if mode == "suite" else dialog.outcome
                if outcome is None or not outcome.ok:
                    raise RuntimeError(f"Documentation plan failed: {mode}: {dialog.result_view.toPlainText()}")
                capture(app, dialog, f"ai-{mode}")
                if mode == "request":
                    dialog.activity_button.setChecked(True)
                    capture(app, dialog, "ai-activity")
                dialog.close()
                dialog.deleteLater()
        settings_dialog = SettingsDialog(QLabel("Demo catalog"), settings_path)
        settings_dialog.usage_page.store = store
        settings_dialog.resize(1100, 760)
        settings_dialog.section_rail.setCurrentRow(1)
        capture(app, settings_dialog, "ai-settings")
        settings_dialog.section_rail.setCurrentRow(2)
        capture(app, settings_dialog, "ai-usage")
        settings_dialog.close()


if __name__ == "__main__":
    main()
