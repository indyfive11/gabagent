"""Cross-room wake arbiter (voice/wake_arbiter.py) — the brain-side first-to-hear referee for the
door-open double-answer. Time is injected (`now=`) so the grace-window / decide / fallback logic is
deterministic with no real sleeping; the store is isolated under a tmp XDG_DATA_HOME."""
import pytest

from gabagent.voice import wake_arbiter as w


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    return w


def test_single_claim_proceeds(store):
    c = store.claim("host", now=100.0, window_secs=0.25)
    v = store.resolve(c["window_id"], "host", now=100.30)
    assert v["verdict"] == "proceed" and v["winner_room"] == "host"


def test_pending_before_window_closes(store):
    c = store.claim("host", now=100.0, window_secs=0.25)
    # asked too early (still inside the grace window) → pending, not yet decided
    assert store.resolve(c["window_id"], "host", now=100.10)["verdict"] == "pending"


def test_two_rooms_earliest_receipt_wins(store):
    a = store.claim("host", now=100.00, window_secs=0.25)
    b = store.claim("satellite", now=100.05, window_secs=0.25)
    assert a["window_id"] == b["window_id"]          # both joined the one open window
    assert store.resolve(a["window_id"], "host", now=100.30)["verdict"] == "proceed"
    assert store.resolve(b["window_id"], "satellite", now=100.30)["verdict"] == "stand_down"


def test_detector_latency_normalization_reorders(store):
    # satellite's wake fires LATER in wall time but its detector is much slower → normalized EARLIER → it wins.
    a = store.claim("host", detector_latency_ms=0.0, now=100.00, window_secs=0.25)
    b = store.claim("satellite", detector_latency_ms=500.0, now=100.05, window_secs=0.25)
    assert store.resolve(a["window_id"], "satellite", now=100.30)["winner_room"] == "satellite"
    assert store.resolve(a["window_id"], "host", now=100.30)["verdict"] == "stand_down"


def test_media_playing_room_wins_close_call(store):
    # host is normalized-earliest, but satellite has local media playing → it must win so the turn can't be
    # grabbed out from under its playback. Still a single winner (no double-answer).
    store.claim("host", now=100.00, window_secs=0.25)
    b = store.claim("satellite", media_playing=True, now=100.10, window_secs=0.25)
    assert store.resolve(b["window_id"], "satellite", now=100.30)["winner_room"] == "satellite"


def test_new_window_after_close_is_separate(store):
    a = store.claim("host", now=100.00, window_secs=0.25)
    c = store.claim("host", now=100.40, window_secs=0.25)   # past the first window's decide_at
    assert a["window_id"] != c["window_id"]


def test_fallback_proceeds_when_winner_never_starts(store):
    a = store.claim("host", now=100.00, window_secs=0.25)
    store.claim("satellite", now=100.05, window_secs=0.25)
    store.resolve(a["window_id"], "satellite", now=100.30)      # decide: host wins, satellite stands down
    # host never committed AND never answered → it never started → satellite un-stands-down (never-zero)
    assert store.check_fallback(a["window_id"], "satellite", now=101.30)["verdict"] == "proceed"


def test_fallback_stays_down_when_winner_committed(store):
    # The winner commits (accepts proceed) in ms — long BEFORE its /respond lands (6-26s). The peer's probe
    # must already see it as taken, else every live-but-slow winner double-answers.
    a = store.claim("host", now=100.00, window_secs=0.25)
    store.claim("satellite", now=100.05, window_secs=0.25)
    store.resolve(a["window_id"], "host", now=100.30)       # host wins
    assert store.mark_committed(a["window_id"], "host", now=100.35) is True   # ~ms after accepting proceed
    # satellite probes at +1s, host's /respond is still 6-26s away → committed keeps satellite down (presence-based)
    assert store.check_fallback(a["window_id"], "satellite", now=101.30)["verdict"] == "answered"


def test_mark_committed_only_the_winner(store):
    a = store.claim("host", now=100.00, window_secs=0.25)
    store.claim("satellite", now=100.05, window_secs=0.25)
    store.resolve(a["window_id"], "host", now=100.30)       # host wins
    assert store.mark_committed(a["window_id"], "satellite", now=100.35) is False  # loser can't commit
    assert store.mark_committed(a["window_id"], "host", now=100.35) is True


def test_heartbeat_liveness_releases_peer_when_commit_goes_stale(store):
    # liveness_secs>0 (heartbeat mode): a winner that committed then went silent past the grace releases the
    # peer (mid-turn death recovery); a fresh commit keeps it down.
    a = store.claim("host", now=100.00, window_secs=0.25)
    store.claim("satellite", now=100.05, window_secs=0.25)
    store.resolve(a["window_id"], "host", now=100.30)
    store.mark_committed(a["window_id"], "host", now=100.35)
    assert store.check_fallback(a["window_id"], "satellite", now=100.80, liveness_secs=1.0)["verdict"] == "answered"
    assert store.check_fallback(a["window_id"], "satellite", now=102.00, liveness_secs=1.0)["verdict"] == "proceed"


def test_fallback_stays_down_when_winner_answered(store):
    a = store.claim("host", now=100.00, window_secs=0.25)
    store.claim("satellite", now=100.05, window_secs=0.25)
    store.resolve(a["window_id"], "satellite", now=100.30)      # host wins
    assert store.mark_answered("host", now=100.60) is True  # host's /respond landed (terminal mark)
    assert store.check_fallback(a["window_id"], "satellite", now=102.80)["verdict"] == "answered"


def test_mark_answered_only_marks_a_window_this_room_won(store):
    a = store.claim("host", now=100.00, window_secs=0.25)
    store.claim("satellite", now=100.05, window_secs=0.25)
    store.resolve(a["window_id"], "host", now=100.30)       # host wins
    assert store.mark_answered("satellite", now=100.60) is False  # satellite didn't win any window
    assert store.mark_answered("host", now=100.60) is True


def test_missing_window_id_fails_open(store):
    assert store.resolve("", "host", now=1.0)["verdict"] == "proceed"
    assert store.check_fallback("", "host", now=1.0)["verdict"] == "proceed"
