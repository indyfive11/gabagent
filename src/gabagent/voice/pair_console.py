"""Operator console for `gab --pair-voice-agent` — the human-accept side of token pairing.

Runs on the BRAIN host. Opens a pairing window on the running brain, shows each front end that asks to
pair — its untrusted, self-reported label next to the AUTHORITATIVE observed source IP — and lets the
operator approve exactly one with a keystroke; the brain then hands that device the bearer token. ALL
human interaction is here on the brain console: the front end stays fully headless (it only POSTs and
polls, never needs a TTY). The security signal the operator trusts is the observed IP, not the label.

`run_pair_console` is pure over an injected `call`/`ask`/`echo`/`sleep`/`now` so it is unit-testable
without a live brain; `run` wires the real urllib transport + stdio and is what the CLI flag invokes.
"""
from __future__ import annotations


def run_pair_console(cfg, *, call, ask, echo, sleep, now, timeout: float = 300.0) -> int:
    """Drive the pairing ceremony. `call(method, path, body) -> (status, dict)` talks to the brain and
    raises ConnectionError if it's unreachable. Returns a process exit code."""
    token = (getattr(cfg, "voice_auth_token", "") or "").strip()
    if not token:
        echo("This brain has no auth token, so there is nothing to hand out.\n"
             "Provision it first:  gabagent-install --enable-voice-host --host <LAN_IP>\n"
             "then restart the brain and re-run this.")
        return 2

    try:
        call("POST", "/pair/open", None)
    except ConnectionError as e:
        echo(f"Can't reach the voice brain ({e}). Is it running?")
        return 2

    echo("Pairing window open. On the NEW device, start its setup so it contacts this brain.")
    echo("Approve ONLY a device whose observed IP you recognise — the label is self-reported.\n")

    deadline = now() + timeout
    while now() < deadline:
        try:
            _, data = call("GET", "/pair/candidates", None)
        except ConnectionError as e:
            echo(f"Lost the brain ({e}); retrying…")
            sleep(1.0)
            continue
        cands = [c for c in (data.get("candidates") or []) if not c.get("accepted")]
        if not cands:
            sleep(1.0)
            continue

        echo("Device(s) requesting to pair:")
        for i, c in enumerate(cands):
            # label is server-sanitised already; !r escapes any residual specials as a second guard.
            echo(f"  [{i}] claims label: {c.get('label')!r}   OBSERVED IP: {c.get('peer_ip')}"
                 f"   room: {c.get('room_id')!r}")
        ans = (ask("Approve which? type the index, or press Enter to keep waiting: ") or "").strip()
        if not ans.isdigit() or int(ans) >= len(cands):
            echo("Nothing approved; still waiting…\n")
            sleep(1.0)
            continue

        pick = cands[int(ans)]
        _, res = call("POST", "/pair/accept", {"client_id": pick["client_id"]})
        if res.get("ok"):
            echo(f"\n✓ Approved {pick.get('label')!r} @ {pick.get('peer_ip')}. "
                 "The device can now retrieve the token.")
            return 0
        echo(f"Accept failed ({res.get('error')}); it may have expired. Still waiting…\n")
        sleep(1.0)

    echo("Pairing window timed out; nothing approved.")
    return 1


def _urllib_transport(base: str, token: str):
    """A real HTTP transport over stdlib urllib (no httpx dependency for this operator command). Maps a
    non-2xx to (status, body) and a connection failure to ConnectionError."""
    import json
    import urllib.error
    import urllib.request

    def call(method, path, body):
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(
            base + path, data=data, method=method,
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                raw = r.read()
                return r.status, (json.loads(raw) if raw else {})
        except urllib.error.HTTPError as e:
            raw = e.read()
            return e.code, (json.loads(raw) if raw else {})
        except urllib.error.URLError as e:
            raise ConnectionError(getattr(e, "reason", e))

    return call


def run(cfg, *, timeout: float = 300.0) -> int:
    """CLI entry: wire the real brain URL (from config), transport, and stdio."""
    import time

    import typer

    host = (getattr(cfg, "voice_host", "") or "127.0.0.1").strip() or "127.0.0.1"
    port = int(getattr(cfg, "voice_port", 8765) or 8765)
    token = (getattr(cfg, "voice_auth_token", "") or "").strip()
    call = _urllib_transport(f"http://{host}:{port}", token)
    return run_pair_console(
        cfg, call=call,
        ask=lambda p: input(p),
        echo=lambda m: typer.echo(m),
        sleep=time.sleep, now=time.monotonic, timeout=timeout,
    )
