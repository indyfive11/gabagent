"""Deferred-announce channel (voice/announce_store.py) — the liveness-leased brain→voice queue.

Time is injected (`now=`) and the lease shrunk so the claim/renew/revert logic is deterministic with no
real sleeping."""
import pytest

from gabagent.voice import announce_store as a


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    return a


def test_enqueue_then_poll_returns_job_id_and_text(store):
    store.enqueue("Tea is ready.", job_id="j1")
    out = store.poll("sX", now=100.0)
    assert out == [{"job_id": "j1", "text": "Tea is ready."}]


def test_empty_text_is_dropped(store):
    assert store.enqueue("   ", job_id="j0") is None
    assert store.poll("sX", now=1.0) == []


def test_ack_removes_item(store):
    store.enqueue("done", job_id="j1")
    assert store.poll("sX", now=1.0)  # claimed
    assert store.ack("j1") is True
    assert store.poll("sX", now=2.0) == []   # gone
    assert store.ack("j1") is False          # idempotent re-ack


def test_assign_once_not_re_returned_but_liveness_holds(store):
    store.enqueue("hi", job_id="j1")
    assert store.poll("sX", now=1.0, lease_secs=5.0) == [{"job_id": "j1", "text": "hi"}]
    # A second poll by the same session does NOT re-hand it (already delivering to sX)…
    assert store.poll("sX", now=2.0, lease_secs=5.0) == []
    # …and while sX KEEPS polling (each poll renews the claim), sY never steals it even far past one lease.
    for ts in (4.0, 6.0, 8.0, 10.0, 12.0):
        store.poll("sX", now=ts, lease_secs=5.0)
    assert store.poll("sY", now=12.5, lease_secs=5.0) == []


def test_gone_claimer_reverts_and_another_session_reclaims(store):
    store.enqueue("hi", job_id="j1")
    assert store.poll("sX", now=0.0, lease_secs=5.0) == [{"job_id": "j1", "text": "hi"}]
    # sX never polls again; sY polls after the lease elapses → sX judged gone, sY reclaims.
    assert store.poll("sY", now=10.0, lease_secs=5.0) == [{"job_id": "j1", "text": "hi"}]
    # sX coming back late does NOT get it (sY now holds it, alive).
    assert store.poll("sX", now=11.0, lease_secs=5.0) == []


def test_originating_first_preferred_session_gets_it(store):
    store.enqueue("ring", job_id="t1", preferred_session="kitchen")
    # A different device polling within the grace does NOT get it — reserved for the originator.
    assert store.poll("office", now=1.0, lease_secs=5.0) == []
    # The originating device gets it immediately.
    assert store.poll("kitchen", now=2.0, lease_secs=5.0) == [{"job_id": "t1", "text": "ring"}]


def test_originating_first_falls_back_after_grace(store):
    store.enqueue("ring", job_id="t1", preferred_session="kitchen", now=0.0)
    # Kitchen never shows up; after the fallback grace any device may take it.
    assert store.poll("office", now=1.0, lease_secs=5.0) == []          # within grace
    assert store.poll("office", now=6.0, lease_secs=5.0) == [{"job_id": "t1", "text": "ring"}]


def test_builder_item_is_free_for_all(store):
    store.enqueue("build done", job_id="b1", source="builder")  # preferred_session=None
    assert store.poll("anydevice", now=1.0, lease_secs=5.0) == [{"job_id": "b1", "text": "build done"}]


def test_pending_lists_items(store):
    store.enqueue("a", job_id="j1")
    store.enqueue("b", job_id="j2")
    ids = {i["job_id"] for i in store.pending()}
    assert ids == {"j1", "j2"}
