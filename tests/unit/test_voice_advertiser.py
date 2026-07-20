"""BrainAdvertiser — gating + fail-soft. These run whether or not `zeroconf` is installed: the loopback
and no-op paths never import it, and the fail-soft path is exactly what we assert when it is absent."""
import socket

import pytest

from gabagent.voice.advertiser import (
    BrainAdvertiser,
    _build_properties,
    _is_loopback,
    _resolve_advertise_ip,
    _SERVICE_TYPE,
)


def test_txt_carries_room_id_and_probe_path():
    props = _build_properties("living_room")
    assert props[b"room_id"] == b"living_room"
    assert props[b"path"] == b"/health"


def test_txt_room_id_empty_for_single_room_install():
    # A single-room install (no voice_room_id) still publishes the key, empty — a satellite reads it as the
    # default/only brain. The key is always present so a browser filter has a deterministic field.
    props = _build_properties("")
    assert props[b"room_id"] == b""


def test_txt_never_carries_a_secret():
    # TXT is broadcast in the clear; guard that we only ever publish the two known-safe keys.
    assert set(_build_properties("kitchen")) == {b"room_id", b"path"}


def test_wire_name_is_the_blessed_constant():
    # Satellites match on this exact string; a change silently breaks discovery. Guard it.
    assert _SERVICE_TYPE == "_voice-brain._tcp.local."


@pytest.mark.parametrize("host", ["", "127.0.0.1", "::1", "localhost", "  127.0.0.1 ", "LOCALHOST"])
def test_loopback_hosts_are_skipped(host):
    assert _is_loopback(host) is True


@pytest.mark.parametrize("host", ["192.0.2.10", "0.0.0.0", "198.51.100.7"])
def test_non_loopback_hosts_advertise(host):
    assert _is_loopback(host) is False


def test_start_on_loopback_is_a_noop_and_never_touches_zeroconf():
    # Returns False before any import, so it holds even with zeroconf installed.
    adv = BrainAdvertiser()
    assert adv.start("127.0.0.1", 8765) is False
    adv.stop()  # idempotent, no-op


def test_explicit_lan_ip_is_used_verbatim():
    assert _resolve_advertise_ip("192.0.2.10") == "192.0.2.10"


def test_bind_all_resolves_to_a_concrete_route_ip():
    ip = _resolve_advertise_ip("0.0.0.0")
    # Either a real egress IP or None (no network) — never the literal bind-all address.
    assert ip != "0.0.0.0"
    if ip is not None:
        socket.inet_aton(ip)  # a parseable IPv4


def test_start_failsoft_when_zeroconf_absent(monkeypatch):
    # Force the import to fail regardless of whether zeroconf is installed → must degrade to False, not raise.
    import builtins

    real_import = builtins.__import__

    def _boom(name, *a, **k):
        if name == "zeroconf" or name.startswith("zeroconf."):
            raise ImportError("simulated: zeroconf not installed")
        return real_import(name, *a, **k)

    monkeypatch.setattr(builtins, "__import__", _boom)
    adv = BrainAdvertiser()
    assert adv.start("192.0.2.10", 8765) is False  # fail-soft, no crash
    adv.stop()


def test_stop_is_idempotent_before_start():
    adv = BrainAdvertiser()
    adv.stop()
    adv.stop()  # second call must also be safe
