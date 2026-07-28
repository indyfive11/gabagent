"""Pairing (token auto-provisioning) — the CLAIM state machine + the HTTP wiring.

The state-machine tests drive gabagent.voice.pairing.PairingState directly with an injected clock and a
deterministic secret factory (no sockets, no time). The HTTP tests exercise the guard exemption and the
full open→register→accept→retrieve flow through the real Starlette app.
"""
import itertools
from types import SimpleNamespace
import pytest

from gabagent.voice.pairing import (
    PairingState, PairOutcome, sanitize_label, MIN_CLIENT_ID_LEN,
)

httpx = pytest.importorskip("httpx")
pytest.importorskip("starlette")

from gabagent.voice.server import build_app
from tests.unit.test_voice_server import make_ctx, _client  # reuse the ctx + ASGI client helpers


CID = "cid-" + "a" * 24        # a >=128-bit-length client_id (passes the length floor)
CID2 = "cid-" + "b" * 24


def _state(window_ttl=300.0, claim_ttl=30.0):
    """A PairingState with a controllable clock and deterministic, unique secrets."""
    t = [0.0]
    counter = itertools.count(1)
    st = PairingState(
        window_ttl=window_ttl, claim_ttl=claim_ttl,
        secret_factory=lambda: f"secret-{next(counter)}",
        clock=lambda: t[0],
    )
    return st, t


def _pair(st, cid=CID, secret=None, peer="10.0.0.5", label="pi", room="raspi", token="THE_TOKEN"):
    return st.pair(client_id=cid, claim_secret=secret, peer_ip=peer,
                   label=label, room_id=room, auth_token=token)


# ----------------------------------------------------------------------------- state machine

def test_register_returns_pending_and_secret_only_when_window_open():
    st, _ = _state()
    # window closed by default → no candidate can register
    assert _pair(st).http == 409
    assert _pair(st).error == "no_pairing_window_open"
    st.open_window()
    out = _pair(st)
    assert (out.http, out.status) == (202, "pending")
    assert out.claim_secret == "secret-1"
    assert out.auth_token is None


def test_secretless_repost_is_idempotent_recovers_dropped_202():
    st, _ = _state()
    st.open_window()
    first = _pair(st)
    # a dropped first 202 → client re-POSTs secretless from the same peer → SAME secret, no rotation
    again = _pair(st)
    assert again.http == 202 and again.claim_secret == first.claim_secret == "secret-1"


def test_short_client_id_rejected():
    st, _ = _state()
    st.open_window()
    out = _pair(st, cid="raspi")
    assert out.http == 400 and out.status == "bad_client_id"
    assert len("raspi") < MIN_CLIENT_ID_LEN


def test_accept_then_retrieve_with_secret_issues_token_and_replays_within_ttl():
    st, t = _state(claim_ttl=30.0)
    st.open_window()
    secret = _pair(st).claim_secret
    assert st.accept(CID) is True
    # retrieval requires the secret from the bound peer
    out = _pair(st, secret=secret)
    assert (out.http, out.status, out.auth_token) == (200, "issued", "THE_TOKEN")
    # replay-until-TTL: a dropped 200 recovers on retry within the claim window
    t[0] = 20.0
    assert _pair(st, secret=secret).auth_token == "THE_TOKEN"


def test_claim_expires_after_ttl():
    st, t = _state(claim_ttl=30.0)
    st.open_window()
    secret = _pair(st).claim_secret
    st.accept(CID)
    t[0] = 31.0
    # the accepted claim timed out → pruned → reads back as no window (a 409 the client re-pairs from)
    out = _pair(st, secret=secret)
    assert out.http == 409 and out.error == "no_pairing_window_open"


def test_different_peer_ip_is_rejected_never_merged():
    st, _ = _state()
    st.open_window()
    _pair(st, peer="10.0.0.5")                       # legit peer registers first
    out = _pair(st, peer="10.0.0.9")                 # imposter, same client_id, different IP
    assert out.http == 409 and out.error == "client_id_registered_from_different_peer"


def test_secretless_repost_in_accepted_phase_returns_secret_for_recovery():
    st, _ = _state()
    st.open_window()
    secret = _pair(st).claim_secret
    st.accept(CID)
    # first 202 was lost AND the operator accepted before recovery → secretless re-POST hands the secret back
    out = _pair(st, secret=None)
    assert (out.http, out.status, out.claim_secret) == (202, "accepted", secret)


def test_wrong_secret_in_accepted_phase_is_forbidden():
    st, _ = _state()
    st.open_window()
    _pair(st)
    st.accept(CID)
    out = _pair(st, secret="not-the-secret")
    assert out.http == 403 and out.error == "bad_claim_secret"


def test_accept_closes_window_and_drops_other_candidates():
    st, _ = _state()
    st.open_window()
    _pair(st, cid=CID, peer="10.0.0.5")
    _pair(st, cid=CID2, peer="10.0.0.6")
    assert len(st.candidates()) == 2
    assert st.accept(CID) is True
    cands = st.candidates()
    assert [c["client_id"] for c in cands] == [CID]        # the other candidate is gone
    assert st.window_open() is False                       # window closed on accept
    # the dropped peer now sees no window
    assert _pair(st, cid=CID2, peer="10.0.0.6").error == "no_pairing_window_open"


def test_accept_unknown_candidate_is_false():
    st, _ = _state()
    st.open_window()
    assert st.accept("nope-nope-nope-nope-nope") is False


def test_sanitize_label_strips_control_bytes_and_clamps():
    assert sanitize_label("living\x1b[31m room\n<inject>") == "living[31m room<inject>"
    assert sanitize_label("x" * 100) == "x" * 32
    assert sanitize_label(None) == ""
    assert sanitize_label("\x00\x07\x7f\x9b") == ""


def test_registered_label_is_sanitized_in_candidate_list():
    st, _ = _state()
    st.open_window()
    _pair(st, label="pi\x1b[2Jroom")
    assert st.candidates()[0]["label"] == "pi[2Jroom"


# ----------------------------------------------------------------------------- HTTP wiring

async def test_pair_501_when_no_token_configured(tmp_path):
    app = build_app(make_ctx(tmp_path, []))     # no voice_auth_token → nothing to hand out
    async with _client(app) as client:
        r = await client.post("/pair", json={"client_id": CID})
        assert r.status_code == 501 and r.json()["error"] == "pairing_unsupported"


async def test_pair_is_guard_exempt_but_admin_routes_are_guarded(tmp_path):
    ctx = make_ctx(tmp_path, [])
    ctx.config.voice_auth_token = "tok"
    app = build_app(ctx)
    async with _client(app) as client:
        # /pair works WITHOUT a bearer (client has no token yet) — guard-exempt. No window → 409, not 401.
        r = await client.post("/pair", json={"client_id": CID})
        assert r.status_code == 409
        # the admin routes are NOT exempt — bearer required (exact-match exemption, no prefix leak)
        assert (await client.post("/pair/open")).status_code == 401
        assert (await client.get("/pair/candidates")).status_code == 401
        assert (await client.post("/pair/accept", json={"client_id": CID})).status_code == 401


async def test_full_http_flow_open_register_accept_retrieve(tmp_path):
    ctx = make_ctx(tmp_path, [])
    ctx.config.voice_auth_token = "THE-BRAIN-TOKEN"
    app = build_app(ctx)
    auth = {"Authorization": "Bearer THE-BRAIN-TOKEN"}
    async with _client(app) as client:
        assert (await client.post("/pair/open", headers=auth)).status_code == 200
        # front end registers (unauthenticated)
        r = await client.post("/pair", json={"client_id": CID, "label": "living-room pi"})
        assert r.status_code == 202 and r.json()["status"] == "pending"
        secret = r.json()["claim_secret"]
        # operator sees the candidate, accepts it
        cands = (await client.get("/pair/candidates", headers=auth)).json()["candidates"]
        assert cands and cands[0]["client_id"] == CID
        assert (await client.post("/pair/accept", json={"client_id": CID}, headers=auth)).status_code == 200
        # front end retrieves the real token with its secret
        got = await client.post("/pair", json={"client_id": CID, "claim_secret": secret})
        assert got.status_code == 200
        assert got.json() == {"auth_token": "THE-BRAIN-TOKEN", "token_scheme": "bearer"}


# ----------------------------------------------------------------------------- install-time mint (§2)

def test_ensure_voice_auth_token_mints_when_absent():
    from gabagent.install.voice_host import ensure_voice_auth_token
    cfg = SimpleNamespace(voice_auth_token="")
    tok, minted = ensure_voice_auth_token(cfg)
    assert minted is True and cfg.voice_auth_token == tok and len(tok) >= 20


def test_ensure_voice_auth_token_is_idempotent_never_rotates():
    from gabagent.install.voice_host import ensure_voice_auth_token
    cfg = SimpleNamespace(voice_auth_token="an-existing-token-value-1234567890")
    tok, minted = ensure_voice_auth_token(cfg)
    assert minted is False and tok == "an-existing-token-value-1234567890"
    assert cfg.voice_auth_token == "an-existing-token-value-1234567890"


# ----------------------------------------------------------------------------- operator console

def _fake_call(candidates_seq, *, open_raises=False, accept_ok=True, recorder=None):
    """A scripted brain transport. `candidates_seq` is popped one GET at a time."""
    def call(method, path, body):
        if recorder is not None:
            recorder.append((method, path, body))
        if path == "/pair/open":
            if open_raises:
                raise ConnectionError("refused")
            return 200, {"ok": True, "window_secs": 300}
        if path == "/pair/candidates":
            return 200, {"candidates": candidates_seq.pop(0) if candidates_seq else []}
        if path == "/pair/accept":
            return (200, {"ok": True, "accepted": body["client_id"]}) if accept_ok else (404, {"ok": False, "error": "no_such_candidate"})
        return 404, {}
    return call


def _console(cfg, call, answers, *, timeout=300.0):
    from gabagent.voice.pair_console import run_pair_console
    out, t, ans = [], [0.0], iter(answers)
    def sleep(dt): t[0] += dt
    rc = run_pair_console(
        cfg, call=call, ask=lambda p: next(ans, ""), echo=out.append,
        sleep=sleep, now=lambda: t[0], timeout=timeout)
    return rc, out


def test_console_refuses_without_token():
    recorder = []
    cfg = SimpleNamespace(voice_auth_token="")
    rc, out = _console(cfg, _fake_call([], recorder=recorder), [])
    assert rc == 2 and recorder == []            # never touches the brain
    assert any("no auth token" in m for m in out)


def test_console_reports_unreachable_brain():
    cfg = SimpleNamespace(voice_auth_token="tok")
    rc, out = _console(cfg, _fake_call([], open_raises=True), [])
    assert rc == 2 and any("Can't reach" in m for m in out)


def test_console_happy_path_operator_approves():
    cfg = SimpleNamespace(voice_auth_token="tok")
    recorder = []
    cand = {"client_id": CID, "label": "pi", "peer_ip": "10.0.0.5", "room_id": "raspi", "accepted": False}
    call = _fake_call([[], [cand]], recorder=recorder)      # first poll empty, then the device shows up
    rc, out = _console(cfg, call, ["0"])                    # operator approves index 0
    assert rc == 0
    assert ("POST", "/pair/accept", {"client_id": CID}) in recorder
    assert any("Approved" in m for m in out)


def test_console_times_out_when_operator_never_approves():
    cfg = SimpleNamespace(voice_auth_token="tok")
    cand = {"client_id": CID, "label": "pi", "peer_ip": "10.0.0.5", "room_id": "raspi", "accepted": False}
    # candidate present every poll; operator presses Enter (declines) each time → deadline reached
    call = _fake_call([[cand]] * 10, recorder=None)
    rc, out = _console(cfg, call, [""] * 10, timeout=3.0)
    assert rc == 1 and any("timed out" in m for m in out)
