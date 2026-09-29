"""Test cases are named explicitly, and the name is asked for before adding.

Adding the same endpoint twice used to produce two cases called
``GET /api/brands``, which made a run report ambiguous.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from api_tester.catalog import load_catalog
from api_tester.suite import suggest_case_name

ROOT = Path(__file__).resolve().parents[1]
CATALOG_PATH = ROOT / "data" / "api_catalog.json"


@pytest.fixture(scope="module")
def catalog():
    return load_catalog(CATALOG_PATH)


class TestSuggestCaseName:
    def test_first_case_uses_the_plain_endpoint_name(self):
        assert suggest_case_name("GET", "/api/brands", []) == "GET /api/brands"

    def test_method_is_upper_cased(self):
        assert suggest_case_name("get", "/api/brands", []) == "GET /api/brands"

    def test_repeat_gets_a_numeric_suffix(self):
        assert (
            suggest_case_name("GET", "/api/brands", ["GET /api/brands"])
            == "GET /api/brands (2)"
        )

    def test_suffix_keeps_counting_past_existing_copies(self):
        existing = ["GET /api/brands", "GET /api/brands (2)", "GET /api/brands (3)"]
        assert suggest_case_name("GET", "/api/brands", existing) == "GET /api/brands (4)"

    def test_unrelated_names_do_not_force_a_suffix(self):
        existing = ["Happy path", "POST /api/brands"]
        assert suggest_case_name("GET", "/api/brands", existing) == "GET /api/brands"

    def test_blank_existing_names_are_ignored(self):
        assert suggest_case_name("GET", "/api/brands", ["", "   "]) == "GET /api/brands"


@pytest.fixture(scope="module")
def qt_app():
    pytest.importorskip("PyQt6")
    from PyQt6.QtWidgets import QApplication

    return QApplication.instance() or QApplication([])


@pytest.fixture
def suite_tab(qt_app, catalog, tmp_path):
    from api_tester.suite_ui import SuiteTab

    return SuiteTab(catalog, tmp_path, tmp_path / "history.json", lambda: {})


def _first_endpoint(catalog):
    return catalog.services[0].endpoints[0]


class TestCaseNamePrompt:
    def test_prompt_offers_a_unique_suggestion(self, monkeypatch, suite_tab, catalog):
        from PyQt6.QtWidgets import QInputDialog

        endpoint = _first_endpoint(catalog)
        seen = {}

        def fake_get_text(parent, title, label, text=""):
            seen["default"] = text
            return "Happy path", True

        monkeypatch.setattr(QInputDialog, "getText", fake_get_text)
        assert suite_tab.prompt_case_name(endpoint) == "Happy path"
        assert seen["default"] == f"{endpoint.method} {endpoint.path}"

    def test_cancel_returns_none_so_no_case_is_added(self, monkeypatch, suite_tab, catalog):
        from PyQt6.QtWidgets import QInputDialog

        endpoint = _first_endpoint(catalog)
        monkeypatch.setattr(QInputDialog, "getText", lambda *a, **k: ("Ignored", False))
        assert suite_tab.prompt_case_name(endpoint) is None
        assert suite_tab.suite.cases == []

    def test_blank_name_falls_back_to_the_suggestion(self, monkeypatch, suite_tab, catalog):
        from PyQt6.QtWidgets import QInputDialog

        endpoint = _first_endpoint(catalog)
        monkeypatch.setattr(QInputDialog, "getText", lambda *a, **k: ("   ", True))
        assert suite_tab.prompt_case_name(endpoint) == f"{endpoint.method} {endpoint.path}"

    def test_name_is_trimmed(self, monkeypatch, suite_tab, catalog):
        from PyQt6.QtWidgets import QInputDialog

        endpoint = _first_endpoint(catalog)
        monkeypatch.setattr(QInputDialog, "getText", lambda *a, **k: ("  Smoke  ", True))
        assert suite_tab.prompt_case_name(endpoint) == "Smoke"


class TestAddCaseNaming:
    def test_supplied_name_is_used(self, suite_tab, catalog):
        endpoint = _first_endpoint(catalog)
        suite_tab.add_case_for_endpoint(endpoint, {}, None, "Happy path")
        assert [case.name for case in suite_tab.suite.cases] == ["Happy path"]

    def test_omitted_name_falls_back_to_a_unique_suggestion(self, suite_tab, catalog):
        endpoint = _first_endpoint(catalog)
        suite_tab.add_case_for_endpoint(endpoint, {}, None)
        suite_tab.add_case_for_endpoint(endpoint, {}, None)
        base = f"{endpoint.method} {endpoint.path}"
        assert [case.name for case in suite_tab.suite.cases] == [base, f"{base} (2)"]

    def test_suggestion_accounts_for_already_added_cases(self, suite_tab, catalog):
        endpoint = _first_endpoint(catalog)
        suite_tab.add_case_for_endpoint(endpoint, {}, None, f"{endpoint.method} {endpoint.path}")
        assert suite_tab.suggested_case_name(endpoint) == (
            f"{endpoint.method} {endpoint.path} (2)"
        )


class TestWorkflowAuthoring:
    def test_case_phase_and_always_run_are_editable(self, suite_tab, catalog):
        endpoint = _first_endpoint(catalog)
        suite_tab.add_case_for_endpoint(endpoint, {}, None, "Delete seeded record")

        suite_tab.case_phase.setCurrentIndex(
            suite_tab.case_phase.findData("cleanup")
        )
        suite_tab.case_always_run.setChecked(True)

        case = suite_tab.suite.cases[0]
        assert case.phase == "cleanup"
        assert case.always_run is True
        assert "[Cleanup]" in suite_tab.case_list.currentItem().text()

    def test_dependency_selector_updates_the_case(self, suite_tab, catalog):
        from PyQt6.QtCore import Qt

        endpoint = _first_endpoint(catalog)
        suite_tab.add_case_for_endpoint(endpoint, {}, None, "Create")
        prerequisite = suite_tab.suite.cases[0]
        suite_tab.add_case_for_endpoint(endpoint, {}, None, "Verify")
        dependent = suite_tab.suite.cases[1]

        dependency = suite_tab.case_dependencies.item(0)
        dependency.setCheckState(Qt.CheckState.Checked)

        assert dependent.depends_on == [prerequisite.id]

    def test_removing_case_clears_dependency_references(self, suite_tab, catalog):
        endpoint = _first_endpoint(catalog)
        suite_tab.add_case_for_endpoint(endpoint, {}, None, "Create")
        prerequisite = suite_tab.suite.cases[0]
        suite_tab.add_case_for_endpoint(endpoint, {}, None, "Verify")
        dependent = suite_tab.suite.cases[1]
        dependent.depends_on = [prerequisite.id]

        suite_tab.case_list.setCurrentRow(0)
        suite_tab.remove_case()

        assert dependent.depends_on == []


class TestBaselineAuthoring:
    def test_last_json_response_can_be_saved_and_cleared(
        self, suite_tab, catalog
    ):
        from api_tester.runner import CaseResult

        endpoint = _first_endpoint(catalog)
        suite_tab.add_case_for_endpoint(endpoint, {}, None, "Baseline")
        case = suite_tab.suite.cases[0]
        suite_tab.results[case.id] = CaseResult(
            case_id=case.id,
            case_name=case.name,
            endpoint_id=endpoint.id,
            method=endpoint.method,
            path=endpoint.path,
            service=endpoint.service,
            passed=True,
            response_body='{"id": 7, "name": "Brand"}',
        )
        suite_tab._show_case_result(suite_tab.results[case.id])

        suite_tab.save_baseline()

        assert case.baseline.enabled is True
        assert case.baseline.document == {"id": 7, "name": "Brand"}
        assert suite_tab.clear_baseline_button.isEnabled()

        suite_tab.clear_baseline()
        assert case.baseline.enabled is False
        assert case.baseline.document is None

    def test_structural_rules_update_case_config(self, suite_tab, catalog):
        endpoint = _first_endpoint(catalog)
        suite_tab.add_case_for_endpoint(endpoint, {}, None, "Rules")
        case = suite_tab.suite.cases[0]

        suite_tab.baseline_ignore_paths.setPlainText(
            "$.updatedAt\n$.values[*].id"
        )
        suite_tab.baseline_ignore_array_order.setChecked(True)
        suite_tab.baseline_identity_keys.setPlainText("$.values=id\n$.items=code")
        suite_tab.baseline_tolerance.setValue(0.01)

        assert case.baseline.ignored_paths == [
            "$.updatedAt",
            "$.values[*].id",
        ]
        assert case.baseline.ignore_array_order is True
        assert case.baseline.array_identity_keys == {
            "$.values": "id",
            "$.items": "code",
        }
        assert case.baseline.numeric_tolerance == pytest.approx(0.01)

    def test_compare_uses_side_by_side_dialog(
        self, monkeypatch, suite_tab, catalog
    ):
        from api_tester.runner import CaseResult
        from api_tester.comparison import ResponseComparison

        endpoint = _first_endpoint(catalog)
        suite_tab.add_case_for_endpoint(endpoint, {}, None, "Compare")
        case = suite_tab.suite.cases[0]
        case.baseline.document = {"id": 1}
        suite_tab.results[case.id] = CaseResult(
            case_id=case.id,
            case_name=case.name,
            endpoint_id=endpoint.id,
            method=endpoint.method,
            path=endpoint.path,
            service=endpoint.service,
            passed=False,
            response_body='{"id": 2}',
        )
        opened = []
        monkeypatch.setattr(
            ResponseComparison, "exec", lambda dialog: opened.append(dialog)
        )

        suite_tab.compare_baseline()

        assert len(opened) == 1
        assert '"id": 1' in opened[0].previous
        assert '"id": 2' in opened[0].current

    def test_compare_uses_live_baseline_after_update(
        self, monkeypatch, suite_tab, catalog
    ):
        from api_tester.runner import CaseResult
        from api_tester.comparison import ResponseComparison

        endpoint = _first_endpoint(catalog)
        suite_tab.add_case_for_endpoint(endpoint, {}, None, "Updated")
        case = suite_tab.suite.cases[0]
        case.baseline.document = {"id": 1}
        suite_tab.results[case.id] = CaseResult(
            case_id=case.id,
            case_name=case.name,
            endpoint_id=endpoint.id,
            method=endpoint.method,
            path=endpoint.path,
            service=endpoint.service,
            passed=False,
            response_body='{"id": 2}',
            baseline_expected='{"id": 1}',
            baseline_actual='{"id": 2}',
        )
        suite_tab.save_baseline()
        opened = []
        monkeypatch.setattr(
            ResponseComparison, "exec", lambda dialog: opened.append(dialog)
        )

        suite_tab.compare_baseline()

        assert '"id": 2' in opened[0].previous

    def test_json_null_cannot_be_saved_as_ambiguous_baseline(
        self, monkeypatch, suite_tab, catalog
    ):
        from PyQt6.QtWidgets import QMessageBox
        from api_tester.runner import CaseResult

        endpoint = _first_endpoint(catalog)
        suite_tab.add_case_for_endpoint(endpoint, {}, None, "Null")
        case = suite_tab.suite.cases[0]
        suite_tab.results[case.id] = CaseResult(
            case_id=case.id,
            case_name=case.name,
            endpoint_id=endpoint.id,
            method=endpoint.method,
            path=endpoint.path,
            service=endpoint.service,
            passed=True,
            response_body="null",
        )
        warnings = []
        monkeypatch.setattr(
            QMessageBox, "warning", lambda *args: warnings.append(args)
        )

        suite_tab.save_baseline()

        assert warnings
        assert case.baseline.document is None
        assert case.baseline.enabled is False
