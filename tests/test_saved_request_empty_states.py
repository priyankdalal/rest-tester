"""Empty-state coverage for the Saved Requests and Collections workspaces.

These tests instantiate the page widgets directly (not the whole
``MainWindow``) so they stay focused on the empty-state behaviour: the
placeholder shown, its object names/properties for central styling, when it
disappears, and that persistence/selection behaviour is unaffected.
"""

from __future__ import annotations

import pytest
from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import QPushButton

from api_tester.saved_requests import (
    RequestCollection,
    SavedRequest,
    SavedRequestStore,
)


@pytest.fixture(scope="module")
def qt_app():
    pytest.importorskip("PyQt6")
    from PyQt6.QtWidgets import QApplication

    return QApplication.instance() or QApplication([])


@pytest.fixture
def store(tmp_path):
    return SavedRequestStore(tmp_path / "saved_requests.json")


@pytest.fixture
def saved_requests_page(qt_app, store):
    from api_tester.saved_requests import SavedRequestsPage

    page = SavedRequestsPage(store)
    # isVisible() only reflects reality once the widget hierarchy is shown;
    # a never-shown top-level widget reports False regardless of setVisible.
    page.show()
    yield page
    page.close()
    page.deleteLater()


@pytest.fixture
def collections_page(qt_app, store):
    from api_tester.saved_requests import CollectionsPage

    page = CollectionsPage(store)
    page.show()
    yield page
    page.close()
    page.deleteLater()


# --- Saved Requests -----------------------------------------------------


def test_saved_requests_empty_state_shown_when_no_requests(saved_requests_page) -> None:
    page = saved_requests_page
    assert page.list.isVisible() is False
    assert page.empty_state.isVisible() is True
    assert page.empty_state.title_label.text() == "No saved requests yet"
    assert "API Explorer" in page.empty_state.guidance_label.text()
    # No action makes sense here: saving happens from the API Explorer, not
    # from this page, so the button must stay hidden.
    assert page.empty_state.action_button.isVisible() is False


def test_saved_requests_empty_state_hides_when_request_saved(saved_requests_page, store) -> None:
    page = saved_requests_page
    store.upsert_request(SavedRequest(endpoint_id="reporting.export", name="Export harvest"))
    page.refresh()
    assert page.list.isVisible() is True
    assert page.empty_state.isVisible() is False
    assert page.list.count() == 1


def test_saved_requests_search_with_no_matches_offers_clear_action(saved_requests_page, store) -> None:
    page = saved_requests_page
    store.upsert_request(SavedRequest(endpoint_id="reporting.export", name="Export harvest"))
    page.refresh()
    page.search.setText("does-not-exist")
    assert page.list.isVisible() is False
    assert page.empty_state.isVisible() is True
    assert page.empty_state.title_label.text() == "No matches"
    assert page.empty_state.action_button.isVisible() is True
    assert page.empty_state.action_button.text() == "Clear search"
    # The contextual action must map to real, existing behaviour: clearing the
    # search box, which re-reveals the saved request.
    page.empty_state.action_button.click()
    assert page.search.text() == ""
    assert page.list.isVisible() is True
    assert page.empty_state.isVisible() is False
    assert page.list.count() == 1


def test_saved_requests_selection_preserved_across_refresh(saved_requests_page, store) -> None:
    page = saved_requests_page
    first = SavedRequest(endpoint_id="ep1", name="First")
    second = SavedRequest(endpoint_id="ep2", name="Second")
    store.upsert_request(first)
    store.upsert_request(second)
    page.refresh()
    # Select the second saved request, then force a refresh: the previously
    # selected id must still be selected and the empty state must stay hidden.
    for index in range(page.list.count()):
        item = page.list.item(index)
        if item.data(Qt.ItemDataRole.UserRole) == second.id:
            page.list.setCurrentItem(item)
            break
    page.refresh()
    assert page.current_id() == second.id
    assert page.empty_state.isVisible() is False


def test_empty_state_object_names_and_properties(saved_requests_page) -> None:
    empty_state = saved_requests_page.empty_state
    assert empty_state.objectName() == "emptyState"
    assert empty_state.icon_label.objectName() == "emptyStateIcon"
    assert empty_state.title_label.property("emptyStateTitle") is True
    assert empty_state.guidance_label.property("emptyStateGuidance") is True
    assert empty_state.action_button.property("emptyStateAction") is True


def test_saved_request_actions_follow_visual_hierarchy(saved_requests_page) -> None:
    buttons = {
        button.text(): button
        for button in saved_requests_page.findChildren(QPushButton)
    }
    assert buttons["Open"].property("primary") is True
    assert buttons["Delete"].property("destructive") is True


# --- Page title/description consistency -----------------------------------


def test_saved_requests_page_has_consistent_title_and_description(saved_requests_page) -> None:
    from PyQt6.QtWidgets import QLabel

    page = saved_requests_page
    title = page.findChild(QLabel, "savedRequestsPageTitle")
    description = page.findChild(QLabel, "savedRequestsPageDescription")
    assert title is not None
    assert title.text() == "Saved Requests"
    assert title.property("pageTitle") is True
    assert title.property("workspaceTitle") is True
    assert description is not None
    assert description.property("pageDescription") is True
    assert description.text()


def test_collections_page_has_consistent_title_and_description(collections_page) -> None:
    page = collections_page
    from PyQt6.QtWidgets import QLabel

    title = page.findChild(QLabel, "collectionsPageTitle")
    description = page.findChild(QLabel, "collectionsPageDescription")
    assert title is not None
    assert title.text() == "Collections"
    assert title.property("pageTitle") is True
    assert title.property("workspaceTitle") is True
    assert description is not None
    assert description.property("pageDescription") is True
    assert description.text()


# --- Collections ----------------------------------------------------------


def test_collections_empty_state_shown_when_no_collections(collections_page) -> None:
    page = collections_page
    assert page.collections.isVisible() is False
    assert page.collections_empty_state.isVisible() is True
    assert page.collections_empty_state.title_label.text() == "No collections yet"
    assert page.collections_empty_state.action_button.isVisible() is True
    assert page.collections_empty_state.action_button.text() == "New Collection"
    # With no collections there is nothing to show in the members pane either.
    assert page.members.isVisible() is False
    assert page.members_empty_state.isVisible() is True
    assert page.members_empty_state.title_label.text() == "No collection selected"
    assert page.members_empty_state.action_button.isVisible() is False


def test_collections_empty_state_action_creates_collection(collections_page, monkeypatch) -> None:
    from PyQt6.QtWidgets import QInputDialog

    page = collections_page
    monkeypatch.setattr(QInputDialog, "getText", staticmethod(lambda *a, **k: ("My Collection", True)))
    # The action button maps to the same behaviour as the toolbar's
    # "New Collection" button (``self.create``); clicking it must create a
    # real collection and make the empty state disappear.
    page.collections_empty_state.action_button.click()
    assert page.collections.isVisible() is True
    assert page.collections_empty_state.isVisible() is False
    assert len(page.store.collections) == 1


def test_collections_members_empty_state_when_collection_has_no_requests(collections_page, store) -> None:
    page = collections_page
    collection = RequestCollection(name="Workflow")
    store.upsert_collection(collection)
    page.refresh()
    assert page.members.isVisible() is False
    assert page.members_empty_state.isVisible() is True
    assert page.members_empty_state.title_label.text() == "No requests in this collection"
    assert page.members_empty_state.action_button.isVisible() is True
    assert page.members_empty_state.action_button.text() == "Add Saved Request"


def test_collections_members_empty_state_hides_when_member_added(collections_page, store) -> None:
    page = collections_page
    request = SavedRequest(endpoint_id="ep1", name="First")
    store.upsert_request(request)
    collection = RequestCollection(name="Workflow")
    store.upsert_collection(collection)
    page.refresh()
    store.add_to_collection(collection.id, request.id)
    page.refresh_members()
    assert page.members.isVisible() is True
    assert page.members_empty_state.isVisible() is False
    assert page.members.count() == 1


def test_collections_selection_and_order_preserved_with_empty_state_toggling(collections_page, store) -> None:
    page = collections_page
    first = RequestCollection(name="Alpha")
    second = RequestCollection(name="Beta")
    store.upsert_collection(first)
    store.upsert_collection(second)
    page.refresh()
    for index in range(page.collections.count()):
        item = page.collections.item(index)
        if item.data(Qt.ItemDataRole.UserRole) == second.id:
            page.collections.setCurrentItem(item)
            break
    page.refresh()
    assert page.current_collection_id() == second.id
    assert page.collections.isVisible() is True
    assert page.collections_empty_state.isVisible() is False


def test_collection_actions_follow_visual_hierarchy(collections_page) -> None:
    buttons = {
        button.text(): button
        for button in collections_page.findChildren(QPushButton)
    }
    assert buttons["Run Collection"].property("primary") is True
    assert buttons["Delete"].property("destructive") is True
    assert buttons["Remove"].property("destructive") is True
