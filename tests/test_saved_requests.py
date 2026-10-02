import pytest

from api_tester.saved_requests import (
    RequestCollection,
    SavedRequest,
    SavedRequestStore,
)


def test_saved_requests_round_trip_without_credentials(tmp_path) -> None:
    path = tmp_path / "saved.db"
    store = SavedRequestStore(path)
    request = SavedRequest(
        endpoint_id="reporting.export",
        name="Export harvest",
        values={
            "path:id": "470",
            "header:Authorization": "Bearer secret",
            "header:x-api-key": "secret-key",
        },
        payload={"format": "xlsx"},
    )
    store.upsert_request(request)
    # Read the database bytes rather than a parsed document: a credential
    # must not survive anywhere on disk, including an index or a free page.
    raw = path.read_bytes().lower()
    for secret in (b"access_token", b"api_key", b"bearer secret", b"secret-key"):
        assert secret not in raw
    restored = SavedRequestStore(path)
    assert restored.requests[request.id].payload == {"format": "xlsx"}
    assert restored.requests[request.id].values == {"path:id": "470"}


def test_collection_preserves_order_and_duplicates(tmp_path) -> None:
    store = SavedRequestStore(tmp_path / "saved.db")
    first = SavedRequest("ep1", "First")
    second = SavedRequest("ep2", "Second")
    store.upsert_request(first)
    store.upsert_request(second)
    collection = RequestCollection("Workflow")
    store.upsert_collection(collection)
    store.add_to_collection(collection.id, first.id)
    store.add_to_collection(collection.id, second.id)
    store.add_to_collection(collection.id, first.id)
    assert [item.id for item in store.collection_requests(collection.id)] == [
        first.id,
        second.id,
        first.id,
    ]


def test_collection_reordering_is_persisted(tmp_path) -> None:
    path = tmp_path / "saved.db"
    store = SavedRequestStore(path)
    first = SavedRequest("ep1", "First")
    second = SavedRequest("ep2", "Second")
    store.upsert_request(first)
    store.upsert_request(second)
    collection = RequestCollection("Workflow", [first.id, second.id])
    store.upsert_collection(collection)
    store.move_in_collection(collection.id, 1, -1)
    restored = SavedRequestStore(path)
    assert restored.collections[collection.id].request_ids == [second.id, first.id]


def test_deleting_request_removes_collection_references(tmp_path) -> None:
    store = SavedRequestStore(tmp_path / "saved.db")
    request = SavedRequest("ep1", "First")
    store.upsert_request(request)
    collection = RequestCollection("Workflow", [request.id])
    store.upsert_collection(collection)
    store.delete_request(request.id)
    assert store.collections[collection.id].request_ids == []


def test_cannot_add_missing_request(tmp_path) -> None:
    store = SavedRequestStore(tmp_path / "saved.db")
    collection = RequestCollection("Workflow")
    store.upsert_collection(collection)
    with pytest.raises(KeyError, match="does not exist"):
        store.add_to_collection(collection.id, "missing")
