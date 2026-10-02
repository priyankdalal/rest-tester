from concurrent.futures import ThreadPoolExecutor

import pytest
from PyQt6 import sip

from api_tester.notifications import (
    CATALOG,
    MAX_ENTRIES,
    REQUEST,
    NotificationCenter,
    notification_center,
    relative_time,
)


def test_shared_notification_store_survives_between_test_modules():
    assert not sip.isdeleted(notification_center)
    assert notification_center.thread() is not None


def test_notifications_copy_routes_and_preserve_newest_first():
    centre = NotificationCenter()
    route = {"endpoint_id": "test.get"}
    first = centre.notify(REQUEST, "Request finished", route=route)
    route["endpoint_id"] = "changed"
    latest = centre.notify(CATALOG, "Catalog reloaded")
    assert first.route == {"endpoint_id": "test.get"}
    assert centre.entries() == [latest, first]
    assert centre.entries(1) == [latest]
    assert centre.is_unread(first)
    assert centre.unread_count() == 2


def test_read_and_clear_emit_only_when_state_changes():
    centre = NotificationCenter()
    changes = []
    centre.changed.connect(lambda: changes.append(centre.unread_count()))
    centre.clear()
    centre.mark_all_read()
    first = centre.notify(REQUEST, "Finished")
    centre.mark_all_read()
    centre.mark_all_read()
    assert centre.entries() == [first]
    assert not centre.is_unread(first)
    centre.clear()
    centre.clear()
    assert changes == [1, 0, 0]
    assert centre.entries() == []


def test_concurrent_notifications_cap_history_and_unread_count():
    centre = NotificationCenter()
    with ThreadPoolExecutor(max_workers=4) as pool:
        entries = list(pool.map(
            lambda index: centre.notify(REQUEST, str(index)),
            range(MAX_ENTRIES + 20),
        ))
    assert len({entry.id for entry in entries}) == MAX_ENTRIES + 20
    retained = centre.entries()
    assert len(retained) == MAX_ENTRIES
    assert centre.unread_count() == MAX_ENTRIES
    assert all(centre.is_unread(entry) for entry in retained)
    dropped = set(entry.id for entry in entries) - set(entry.id for entry in retained)
    assert all(not centre.is_unread(entry) for entry in entries if entry.id in dropped)


@pytest.mark.parametrize(("offset", "expected"), [
    (0, "just now"),
    (-300, "5 min ago"),
    (-3600, "1 hour ago"),
    (-259200, "3 days ago"),
    (600, ""),
])
def test_relative_notification_times(offset, expected):
    assert relative_time(1_000_000 + offset, now=1_000_000) == expected


def test_unknown_timestamp_is_not_displayed():
    assert relative_time(0, now=1_000_000) == ""
