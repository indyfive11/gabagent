# TIDAL voice skill — setup (Mopidy + mopidy-tidal)

The `tidal.*` voice commands drive a local **Mopidy** server with the **mopidy-tidal** backend over
its HTTP JSON-RPC API (`http://localhost:6680/mopidy/rpc`). Until Mopidy is running and authorized,
the skill stays invisible (the provider's `detect()` fails closed, so nothing is published — no
errors). Once it's up, gabagent discovers it automatically on the next voice start / `rescan`.

Requires an active TIDAL subscription.

## 1. Install (CachyOS / Arch)

Mopidy depends on GStreamer + PyGObject (system libraries), so installing Mopidy from the AUR is the
least painful path — it pulls the right gstreamer plugins:

```bash
paru -S mopidy            # or: yay -S mopidy
# codecs for AAC/FLAC/DASH streams:
sudo pacman -S gst-plugins-good gst-plugins-bad gst-plugins-ugly gst-libav
```

Then add the TIDAL backend **into Mopidy's Python environment** (mopidy-tidal must be importable by
the same interpreter Mopidy runs as — for the AUR system package that's system Python):

```bash
sudo pip install --break-system-packages Mopidy-Tidal
# (alternatively, if you prefer an isolated stack: pipx install mopidy && pipx inject mopidy Mopidy-Tidal,
#  but pipx can miss system gi/gstreamer — the AUR route above avoids that.)
```

Verify the backend is seen:

```bash
mopidy deps | grep -i tidal     # should list Mopidy-Tidal and python-tidalapi
```

## 2. Configure  `~/.config/mopidy/mopidy.conf`

```ini
[http]
enabled = true
hostname = 127.0.0.1
port = 6680

[tidal]
enabled = true
quality = LOSSLESS        ; HiFi FLAC. (HI_RES_LOSSLESS needs auth_method = PKCE)
auth_method = OAUTH

[audio]
; PipeWire/Pulse on KDE: usually auto. If you get no sound, uncomment:
; output = pulsesink
```

## 3. Authorize TIDAL (one-time OAuth)

Run Mopidy in the foreground and watch for the login link:

```bash
mopidy
# In the logs you'll see a line like:
#   Visit https://link.tidal.com/XXXXX to log in, the code will expire in ...
```

Open that URL in a browser, approve, and Mopidy finishes login. The token is cached
(`~/.local/share/mopidy/tidal/`), so this is a one-time step.

## 4. Run it (leave it running)

Foreground for now:

```bash
mopidy
```

Or as a user service so it's always available:

```bash
systemctl --user enable --now mopidy    # if the AUR package ships a user unit
```

## 5. Verify the API (what the skill calls)

```bash
curl -s -X POST -H 'Content-Type: application/json' \
  -d '{"jsonrpc":"2.0","id":1,"method":"core.get_version"}' \
  http://localhost:6680/mopidy/rpc
# -> {"jsonrpc":"2.0","id":1,"result":"3.x.y"}
```

A live search smoke test:

```bash
curl -s -X POST -H 'Content-Type: application/json' \
  -d '{"jsonrpc":"2.0","id":1,"method":"core.library.search","params":{"query":{"any":["miles davis"]}}}' \
  http://localhost:6680/mopidy/rpc | head -c 400
```

## What you can then say by voice

- "play some Miles Davis on TIDAL" / "play Kind of Blue" — search + queue + play
- "pause" · "resume" · "next" · "previous" · "stop"
- "what's playing?"
- "search TIDAL for Radiohead" (lists results the model can then play)

All `tidal.*` commands are **Tier 1** (frictionless media control — no confirm).

## Config override (optional)

If Mopidy runs elsewhere, set it in `~/.config/gabagent/settings.json`:

```json
"tidal": { "enabled": true, "rpc_url": "http://localhost:6680/mopidy/rpc" }
```

## Notes / gotchas

- **Same-speaker audio:** music plays through your speakers, which the mic can hear. Half-duplex only
  mutes during Aria's TTS, so loud playback could be transcribed as phantom turns. If that bites,
  voice-agent has a small fix queued (wake-word gating / push-to-talk during media).
- If `detect()` never fires, check `curl` step 5 works and that gabagent re-discovered (restart voice
  or say "rescan your capabilities").
