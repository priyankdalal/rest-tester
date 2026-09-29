"""Saved requests and ordered request collections.

Saved requests never contain credentials. They use the active environment's
access token and API key only when loaded or run.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from collections.abc import Callable
from typing import Any
from uuid import uuid4

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtWidgets import (
    QAbstractItemView,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPushButton,
    QSplitter,
    QVBoxLayout,
    QWidget,
)

from .icons import icon
# Re-exported: this widget used to live here, and pages still import it
# from this module.
from .widgets import EmptyStateWidget
from .workspace_store import WorkspaceStore, as_store


def _timestamp() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _safe_values(values: dict[str, Any]) -> dict[str, str]:
    secret_headers = {
        "authorization", "x-api-key", "api-key", "proxy-authorization",
        "x-functions-key", "ocp-apim-subscription-key", "cookie", "set-cookie",
    }
    return {
        str(name): str(item)
        for name, item in values.items()
        if not (
            str(name).lower().startswith("header:")
            and str(name).split(":", 1)[1].lower() in secret_headers
        )
    }


@dataclass
class SavedRequest:
    endpoint_id: str
    name: str
    values: dict[str, str] = field(default_factory=dict)
    payload: Any = None
    expected_status: str = "200-299"
    id: str = field(default_factory=lambda: uuid4().hex[:12])
    created_at: str = field(default_factory=_timestamp)
    updated_at: str = field(default_factory=_timestamp)
    authentication: str = "inherit"

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "SavedRequest":
        return cls(
            id=str(value.get("id") or uuid4().hex[:12]),
            endpoint_id=str(value["endpoint_id"]),
            name=str(value.get("name") or value["endpoint_id"]),
            values=_safe_values(value.get("values", {})),
            payload=value.get("payload"),
            expected_status=str(value.get("expected_status", "200-299")),
            created_at=str(value.get("created_at") or _timestamp()),
            updated_at=str(value.get("updated_at") or _timestamp()),
            authentication=str(value.get("authentication", "inherit")),
        )

    def to_dict(self) -> dict[str, Any]:
        document = asdict(self)
        document["values"] = _safe_values(self.values)
        # Defense in depth if a future caller passes environment-shaped values.
        for key in ("access_token", "api_key", "authorization", "x-api-key"):
            document.pop(key, None)
        return document


@dataclass
class RequestCollection:
    name: str
    request_ids: list[str] = field(default_factory=list)
    id: str = field(default_factory=lambda: uuid4().hex[:12])
    created_at: str = field(default_factory=_timestamp)
    updated_at: str = field(default_factory=_timestamp)

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "RequestCollection":
        return cls(
            id=str(value.get("id") or uuid4().hex[:12]),
            name=str(value.get("name") or "Collection"),
            request_ids=[str(item) for item in value.get("request_ids", [])],
            created_at=str(value.get("created_at") or _timestamp()),
            updated_at=str(value.get("updated_at") or _timestamp()),
        )


class SavedRequestStore:
    """Saved requests and collections, kept in the workspace database.

    The in-memory dictionaries stay the read model the UI binds to, but a
    mutation now writes only the row it touched instead of rewriting the
    whole document, so a crash mid-save can no longer lose the library.
    """

    def __init__(self, source: "WorkspaceStore | Path | str") -> None:
        self.store = as_store(source)
        self.path = self.store.path
        self.requests: dict[str, SavedRequest] = {}
        self.collections: dict[str, RequestCollection] = {}
        self.load()

    def load(self) -> None:
        self.requests = {
            item.id: item
            for item in (
                SavedRequest.from_dict(value)
                for value in self.store.load_saved_requests()
            )
        }
        self.collections = {
            item.id: item
            for item in (
                RequestCollection.from_dict(value)
                for value in self.store.load_collections()
            )
        }

    def save(self) -> None:
        """Writes every in-memory record back.

        Mutators persist themselves, so this is only needed after editing
        the dictionaries directly.
        """
        for request in self.requests.values():
            self.store.upsert_saved_request(request.to_dict())
        for collection in self.collections.values():
            self.store.upsert_collection(asdict(collection))

    def upsert_request(self, request: SavedRequest) -> None:
        request.updated_at = _timestamp()
        self.requests[request.id] = request
        self.store.upsert_saved_request(request.to_dict())

    def delete_request(self, request_id: str) -> None:
        self.requests.pop(request_id, None)
        for collection in self.collections.values():
            collection.request_ids = [
                item for item in collection.request_ids if item != request_id
            ]
        self.store.delete_saved_request(request_id)

    def upsert_collection(self, collection: RequestCollection) -> None:
        collection.updated_at = _timestamp()
        self.collections[collection.id] = collection
        self.store.upsert_collection(asdict(collection))

    def delete_collection(self, collection_id: str) -> None:
        self.collections.pop(collection_id, None)
        self.store.delete_collection(collection_id)

    def add_to_collection(self, collection_id: str, request_id: str) -> None:
        collection = self.collections[collection_id]
        if request_id not in self.requests:
            raise KeyError(f"Saved request does not exist: {request_id}")
        collection.request_ids.append(request_id)
        self.upsert_collection(collection)

    def remove_from_collection(self, collection_id: str, index: int) -> None:
        collection = self.collections[collection_id]
        if 0 <= index < len(collection.request_ids):
            collection.request_ids.pop(index)
            self.upsert_collection(collection)

    def move_in_collection(self, collection_id: str, index: int, offset: int) -> None:
        collection = self.collections[collection_id]
        target = index + offset
        if not 0 <= index < len(collection.request_ids) or not 0 <= target < len(
            collection.request_ids
        ):
            return
        collection.request_ids[index], collection.request_ids[target] = (
            collection.request_ids[target],
            collection.request_ids[index],
        )
        self.upsert_collection(collection)

    def collection_requests(self, collection_id: str) -> list[SavedRequest]:
        collection = self.collections[collection_id]
        return [
            self.requests[request_id]
            for request_id in collection.request_ids
            if request_id in self.requests
        ]


class SavedRequestsPage(QWidget):
    request_selected = pyqtSignal(str)

    def __init__(self, store: SavedRequestStore, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.store = store
        layout = QVBoxLayout(self)
        heading = QLabel("Saved Requests")
        heading.setObjectName("savedRequestsPageTitle")
        heading.setProperty("pageTitle", True)
        heading.setProperty("workspaceTitle", True)
        layout.addWidget(heading)
        description = QLabel("Reuse and manage requests you have saved from the API Explorer.")
        description.setObjectName("savedRequestsPageDescription")
        description.setProperty("pageDescription", True)
        description.setWordWrap(True)
        layout.addWidget(description)
        self.search = QLineEdit()
        self.search.setPlaceholderText("Search saved requests...")
        self.search.textChanged.connect(self.refresh)
        layout.addWidget(self.search)
        self.list = QListWidget()
        self.list.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.list.itemDoubleClicked.connect(self._open)
        layout.addWidget(self.list, 1)
        self.empty_state = EmptyStateWidget()
        layout.addWidget(self.empty_state, 1)
        buttons = QHBoxLayout()
        open_button = QPushButton("Open")
        open_button.setProperty("primary", True)
        open_button.clicked.connect(self._open)
        rename_button = QPushButton("Rename")
        rename_button.clicked.connect(self.rename)
        delete_button = QPushButton("Delete")
        delete_button.setProperty("destructive", True)
        delete_button.clicked.connect(self.delete)
        buttons.addWidget(open_button)
        buttons.addWidget(rename_button)
        buttons.addWidget(delete_button)
        buttons.addStretch()
        layout.addLayout(buttons)
        self.refresh()

    def refresh(self) -> None:
        query = self.search.text().strip().lower()
        selected = self.current_id()
        self.list.clear()
        for request in sorted(self.store.requests.values(), key=lambda item: item.name.lower()):
            if query and query not in f"{request.name} {request.endpoint_id}".lower():
                continue
            item = QListWidgetItem(f"{request.name}\n{request.endpoint_id}")
            item.setData(Qt.ItemDataRole.UserRole, request.id)
            self.list.addItem(item)
            if request.id == selected:
                self.list.setCurrentItem(item)
        self._refresh_empty_state(has_query=bool(query))

    def _refresh_empty_state(self, *, has_query: bool) -> None:
        if self.list.count():
            self.list.setVisible(True)
            self.empty_state.setVisible(False)
            return
        self.list.setVisible(False)
        self.empty_state.setVisible(True)
        if has_query:
            self.empty_state.set_content(
                icon_name="search",
                title="No matches",
                guidance="No saved requests match your search.",
                action_text="Clear search",
                action_callback=self._clear_search,
            )
        else:
            self.empty_state.set_content(
                icon_name="bookmark",
                title="No saved requests yet",
                guidance="Save a request from the API Explorer to reuse it here.",
            )

    def _clear_search(self) -> None:
        self.search.clear()

    def current_id(self) -> str:
        item = self.list.currentItem()
        return str(item.data(Qt.ItemDataRole.UserRole)) if item else ""

    def _open(self, *_args) -> None:
        request_id = self.current_id()
        if request_id:
            self.request_selected.emit(request_id)

    def rename(self) -> None:
        request_id = self.current_id()
        if not request_id:
            return
        request = self.store.requests[request_id]
        name, accepted = QInputDialog.getText(
            self, "Rename saved request", "Name", text=request.name
        )
        if accepted and name.strip():
            request.name = name.strip()
            self.store.upsert_request(request)
            self.refresh()

    def delete(self) -> None:
        request_id = self.current_id()
        if request_id:
            self.store.delete_request(request_id)
            self.refresh()


class CollectionsPage(QWidget):
    run_collection_requested = pyqtSignal(list)

    def __init__(self, store: SavedRequestStore, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.store = store
        layout = QVBoxLayout(self)
        heading = QLabel("Collections")
        heading.setObjectName("collectionsPageTitle")
        heading.setProperty("pageTitle", True)
        heading.setProperty("workspaceTitle", True)
        layout.addWidget(heading)
        description = QLabel("Group saved requests into ordered collections and run them together.")
        description.setObjectName("collectionsPageDescription")
        description.setProperty("pageDescription", True)
        description.setWordWrap(True)
        layout.addWidget(description)
        splitter = QSplitter()
        collections_pane = QWidget()
        collections_pane_layout = QVBoxLayout(collections_pane)
        collections_pane_layout.setContentsMargins(0, 0, 0, 0)
        self.collections = QListWidget()
        self.collections.currentItemChanged.connect(self.refresh_members)
        collections_pane_layout.addWidget(self.collections, 1)
        self.collections_empty_state = EmptyStateWidget()
        collections_pane_layout.addWidget(self.collections_empty_state, 1)
        splitter.addWidget(collections_pane)
        members_pane = QWidget()
        members_pane_layout = QVBoxLayout(members_pane)
        members_pane_layout.setContentsMargins(0, 0, 0, 0)
        self.members = QListWidget()
        members_pane_layout.addWidget(self.members, 1)
        self.members_empty_state = EmptyStateWidget()
        members_pane_layout.addWidget(self.members_empty_state, 1)
        splitter.addWidget(members_pane)
        splitter.setSizes([300, 700])
        layout.addWidget(splitter, 1)
        collection_buttons = QHBoxLayout()
        for caption, slot in (
            ("New Collection", self.create),
            ("Rename", self.rename),
            ("Delete", self.delete),
            ("Add Saved Request", self.add_request),
            ("Remove", self.remove_request),
            ("Up", lambda: self.move_request(-1)),
            ("Down", lambda: self.move_request(1)),
            ("Run Collection", self.run),
        ):
            button = QPushButton(caption)
            if caption == "Run Collection":
                button.setProperty("primary", True)
            elif caption in {"Delete", "Remove"}:
                button.setProperty("destructive", True)
            button.clicked.connect(slot)
            collection_buttons.addWidget(button)
        layout.addLayout(collection_buttons)
        self.refresh()

    def current_collection_id(self) -> str:
        item = self.collections.currentItem()
        return str(item.data(Qt.ItemDataRole.UserRole)) if item else ""

    def refresh(self) -> None:
        selected = self.current_collection_id()
        self.collections.clear()
        for collection in sorted(
            self.store.collections.values(), key=lambda item: item.name.lower()
        ):
            item = QListWidgetItem(collection.name)
            item.setData(Qt.ItemDataRole.UserRole, collection.id)
            self.collections.addItem(item)
            if collection.id == selected:
                self.collections.setCurrentItem(item)
        if self.collections.currentItem() is None and self.collections.count():
            self.collections.setCurrentRow(0)
        if self.collections.count():
            self.collections.setVisible(True)
            self.collections_empty_state.setVisible(False)
        else:
            self.collections.setVisible(False)
            self.collections_empty_state.setVisible(True)
            self.collections_empty_state.set_content(
                icon_name="folder",
                title="No collections yet",
                guidance="Group saved requests into a collection to run them together.",
                action_text="New Collection",
                action_callback=self.create,
            )
        self.refresh_members()

    def refresh_members(self, *_args) -> None:
        self.members.clear()
        collection_id = self.current_collection_id()
        if not collection_id:
            self.members.setVisible(False)
            self.members_empty_state.setVisible(True)
            self.members_empty_state.set_content(
                icon_name="bookmark",
                title="No collection selected",
                guidance="Select or create a collection to see its saved requests.",
            )
            return
        collection = self.store.collections[collection_id]
        for request_id in collection.request_ids:
            request = self.store.requests.get(request_id)
            text = request.name if request else f"Missing request: {request_id}"
            item = QListWidgetItem(text)
            item.setData(Qt.ItemDataRole.UserRole, request_id)
            if request is None:
                item.setForeground(Qt.GlobalColor.red)
            self.members.addItem(item)
        if self.members.count():
            self.members.setVisible(True)
            self.members_empty_state.setVisible(False)
        else:
            self.members.setVisible(False)
            self.members_empty_state.setVisible(True)
            self.members_empty_state.set_content(
                icon_name="bookmark",
                title="No requests in this collection",
                guidance="Add a saved request to build this collection.",
                action_text="Add Saved Request",
                action_callback=self.add_request,
            )

    def create(self) -> None:
        name, accepted = QInputDialog.getText(self, "New collection", "Name")
        if accepted and name.strip():
            self.store.upsert_collection(RequestCollection(name=name.strip()))
            self.refresh()

    def rename(self) -> None:
        collection_id = self.current_collection_id()
        if not collection_id:
            return
        collection = self.store.collections[collection_id]
        name, accepted = QInputDialog.getText(
            self, "Rename collection", "Name", text=collection.name
        )
        if accepted and name.strip():
            collection.name = name.strip()
            self.store.upsert_collection(collection)
            self.refresh()

    def delete(self) -> None:
        collection_id = self.current_collection_id()
        if collection_id:
            self.store.delete_collection(collection_id)
            self.refresh()

    def add_request(self) -> None:
        collection_id = self.current_collection_id()
        requests = sorted(self.store.requests.values(), key=lambda item: item.name.lower())
        if not collection_id or not requests:
            QMessageBox.information(
                self, "No saved requests", "Save a request before adding it to a collection."
            )
            return
        labels = [f"{item.name} - {item.endpoint_id}" for item in requests]
        selected, accepted = QInputDialog.getItem(
            self, "Add saved request", "Request", labels, 0, False
        )
        if accepted and selected in labels:
            self.store.add_to_collection(
                collection_id, requests[labels.index(selected)].id
            )
            self.refresh_members()

    def remove_request(self) -> None:
        collection_id = self.current_collection_id()
        if collection_id:
            self.store.remove_from_collection(collection_id, self.members.currentRow())
            self.refresh_members()

    def move_request(self, offset: int) -> None:
        collection_id = self.current_collection_id()
        index = self.members.currentRow()
        if collection_id:
            self.store.move_in_collection(collection_id, index, offset)
            self.refresh_members()
            self.members.setCurrentRow(index + offset)

    def run(self) -> None:
        collection_id = self.current_collection_id()
        if not collection_id:
            return
        request_ids = [
            item.id for item in self.store.collection_requests(collection_id)
        ]
        if request_ids:
            self.run_collection_requested.emit(request_ids)
