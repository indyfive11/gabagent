"""BrainAdvertiser — gating + fail-soft. These run whether or not `zeroconf` is installed: the loopback
and no-op paths never import it, and the fail-soft path is exactly what we assert when it is absent."""
import socket

import pytest

from gabagent.voice.advertiser import (
    BrainAdvertiser,
    _build_properties,
    _instance_name,
    _is_loopback,
    _label_safe,
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


async def test_start_on_loopback_is_a_noop_and_never_touches_zeroconf():
    # Returns False before any import, so it holds even with zeroconf installed.
    adv = BrainAdvertiser()
    assert await adv.start("127.0.0.1", 8765) is False
    await adv.stop()  # idempotent, no-op


def test_explicit_lan_ip_is_used_verbatim():
    assert _resolve_advertise_ip("192.0.2.10") == "192.0.2.10"


def test_bind_all_resolves_to_a_concrete_route_ip():
    ip = _resolve_advertise_ip("0.0.0.0")
    # Either a real egress IP or None (no network) — never the literal bind-all address.
    assert ip != "0.0.0.0"
    if ip is not None:
        socket.inet_aton(ip)  # a parseable IPv4


async def test_start_registers_through_the_async_zeroconf_api(monkeypatch):
    # REGRESSION (live-verified on hardware, 2026-07-21): the first cut used the SYNC `Zeroconf`. From inside
    # a running asyncio loop — the only way serve_voice ever calls this — `register_service()` raises
    # `EventLoopBlocked`, the fail-soft `except` swallowed it, and the brain silently advertised NOTHING while
    # looking healthy. Pin the async path: a revert to the sync API fails here instead of on the wire.
    aio = pytest.importorskip("zeroconf.asyncio")
    calls = {}

    class _FakeAsyncZeroconf:
        async def async_register_service(self, info):
            calls["registered"] = info

        async def async_unregister_service(self, info):
            calls["unregistered"] = info

        async def async_close(self):
            calls["closed"] = True

    monkeypatch.setattr(aio, "AsyncZeroconf", _FakeAsyncZeroconf)
    adv = BrainAdvertiser()
    assert await adv.start("192.0.2.10", 8765, room_id="kitchen") is True
    info = calls["registered"]
    assert info.port == 8765
    assert info.type == _SERVICE_TYPE
    assert info.properties[b"room_id"] == b"kitchen"
    await adv.stop()
    assert calls["unregistered"] is info and calls["closed"] is True


async def test_start_failsoft_when_zeroconf_absent(monkeypatch):
    # Force the import to fail regardless of whether zeroconf is installed → must degrade to False, not raise.
    import builtins

    real_import = builtins.__import__

    def _boom(name, *a, **k):
        if name == "zeroconf" or name.startswith("zeroconf."):
            raise ImportError("simulated: zeroconf not installed")
        return real_import(name, *a, **k)

    monkeypatch.setattr(builtins, "__import__", _boom)
    adv = BrainAdvertiser()
    assert await adv.start("192.0.2.10", 8765) is False  # fail-soft, no crash
    await adv.stop()


# --- instance naming: two brains on one box must not collide -----------------------------------

def test_two_brains_on_one_host_get_distinct_instance_names():
    # The collision that motivated this: process-per-room puts a room brain and the LAN brain on the SAME
    # box. python-zeroconf's register_service defaults to allow_name_change=False, so a duplicate name raises
    # and the fail-soft `except` swallows it — the second brain silently never advertises.
    assert _instance_name("raspi", 8765) != _instance_name("living_room", 8765)


def test_instance_name_falls_back_to_the_bind_port_when_the_room_is_empty():
    # Two unnamed brains still differ: they cannot share a port.
    a, b = _instance_name("", 8765), _instance_name("", 8766)
    assert a != b
    assert a.endswith("-8765") and b.endswith("-8766")


def test_instance_name_is_a_legal_hostname_label():
    # It is also published as `<name>.local.` (the `server=` field), so a room key with spaces or a slash
    # must not produce an illegal host label.
    name = _instance_name("living room/2", 8765)
    assert all(c.isalnum() or c == "-" for c in name)
    assert not name.startswith("-") and not name.endswith("-")


@pytest.mark.parametrize(
    "raw,expected",
    [("living room", "living-room"), ("  kitchen  ", "kitchen"), ("--x--", "x"), ("", ""), ("a/b", "a-b")],
)
def test_label_safe(raw, expected):
    assert _label_safe(raw) == expected


async def test_stop_is_idempotent_before_start():
    adv = BrainAdvertiser()
    await adv.stop()
    await adv.stop()  # second call must also be safe
