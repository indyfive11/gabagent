# Installing & Configuring gabagent

Two ways to install, a first-run setup wizard, a config reference, and — for the voice
brain — how to run a LAN voice host and pair devices to it.

- [Install](#install)
- [First-run setup](#first-run-setup)
- [Configuration](#configuration)
- [Running a LAN voice host](#running-a-lan-voice-host)
- [Pairing a voice device](#pairing-a-voice-device)

---

## Install

### Quick — from PyPI

```bash
pip install gabagent
```

Then run `gab` (see [First-run setup](#first-run-setup)). For JS-rendered page fetching, also
`playwright install chromium` (optional).

### Guided — from source with the installer

If you're working from a checkout, `bootstrap.sh` runs a small guided installer. It provisions the
Python environment with [`uv`](https://docs.astral.sh/uv/) (installing `uv` first if needed — only
unprompted with `--yes`), then launches the setup wizard:

```bash
git clone https://github.com/indyfive11/gabagent
cd gabagent
./bootstrap.sh            # add --yes to auto-accept a uv install
```

`bootstrap.sh` runs **in place** (it does not re-clone) and hands off to the wizard
(`python -m gabagent.install`, also exposed as the `gabagent-install` command).

The wizard:

1. Offers a **role** menu. Only the **Workstation** (text/coding) role is built today; the
   voice-host / satellite / laptop roles are later-phase and the menu says so.
2. **Detects your box** — distro family, package manager, and GPU vendor — and reports it. Nothing
   is written during detection.
3. Offers to install any **missing system tools** (e.g. `ripgrep`) with the right package-manager
   command for your distro. This is optional; you can decline and still finish setup.
4. Runs the **backend picker** (below) and writes `settings.json`.

> **Note:** `bootstrap.sh`/`uv` and the `python3 -m venv .venv && pip install -e .` dev path are two
> different environments. Use one or the other; the installer uses `uv`.

---

## First-run setup

The backend picker runs automatically the first time you start `gab` with no backend configured (and
as the last wizard step). Pick the "brain" that answers:

| Backend | What it is | You provide |
|---|---|---|
| **Gab AI** (default) | Models on the Gab AI Developer API (default model `arya`). Requires Gab AI Plus. | An API key from https://gab.ai/settings |
| **Claude** | Anthropic API | An Anthropic API key (or `ANTHROPIC_API_KEY` in the env) |
| **Local** | A local [Ollama](https://ollama.com) model | An Ollama model name — nothing is downloaded for you |

The choice is saved to `settings.json`. You can switch later by re-running setup, or, for Claude,
`gab --set-claude-key <key>` (saves the key, switches backend, exits).

To confirm things work:

```bash
gab                          # interactive REPL
gab "list files in src/"     # one-shot
gab --version
gab --credits                # Gab AI credit balance + low-balance guard state
gab --models                 # refresh + inspect the model catalog / router ladder
```

---

## Configuration

Settings live at `~/.config/gabagent/settings.json` (or `$XDG_CONFIG_HOME/gabagent/settings.json`).
The file is plain JSON and safe to hand-edit.

### Precedence

Every setting resolves in this order — **higher wins**:

```
CLI flag / explicit override   >   environment (GABAI_*)   >   settings.json   >   built-in default
```

So an environment variable overrides the saved file, and a CLI flag overrides both. This lets a
deployment supply values purely through the environment — e.g. a systemd `EnvironmentFile` — without
committing them to `settings.json`.

Two rules worth knowing:

- **An empty environment variable is treated as _unset_.** `GABAI_VOICE_AUTH_TOKEN=` (blank) does not
  disable a token configured elsewhere and does not flip a boolean — a stray blank export is a no-op,
  not an override.
- **Env-only secrets are not written back.** If a value (e.g. an auth token) is present only in the
  environment, a later config save does **not** copy it into `settings.json`, so the plaintext file
  never accumulates a second copy of a secret you chose to keep in the environment.

Each `GABAI_*` variable maps to the matching top-level field (uppercased): `api_key` ↔ `GABAI_API_KEY`,
`model` ↔ `GABAI_MODEL`, `voice_host` ↔ `GABAI_VOICE_HOST`, and so on.

### Common fields

| Field | Env var | Meaning |
|---|---|---|
| `api_key` | `GABAI_API_KEY` | Backend API key |
| `model` | `GABAI_MODEL` | Model name (Gab AI default: `arya`) |
| `voice_port` | `GABAI_VOICE_PORT` | Voice brain port (default `8765`) |
| `voice_host` | `GABAI_VOICE_HOST` | Voice brain bind address (default `127.0.0.1`; a LAN IP exposes it to satellites) |
| `voice_auth_token` | `GABAI_VOICE_AUTH_TOKEN` | Bearer token a LAN voice brain requires and hands out on pairing |

`permissions.allow` / `permissions.deny`, `hooks`, `mcp_servers`, and `lsp.servers` are also
configured here — see the project README.

---

## Running a LAN voice host

By default the voice brain binds loopback:

```bash
gab --voice-serve            # binds 127.0.0.1:8765 — local only
```

To serve a satellite on the LAN, the brain needs a concrete LAN bind address **and** a bearer token
(a LAN brain with no token refuses to hand one out). The voice-host provisioning step writes both and
mints the token:

```bash
gabagent-install --enable-voice-host --host <LAN_IP> [--room-id <room>]
# e.g. --host 192.0.2.10
```

This sets `voice_advertise = true` and `voice_host = <LAN_IP>`, and mints `voice_auth_token` if one
isn't already set (it never rotates an existing token; the secret value is never printed). It refuses
a loopback or wildcard address — a LAN host must be a real LAN IP. Then start (or restart) the brain:

```bash
gab --voice-serve            # now binds <LAN_IP>:8765 and enforces bearer auth
```

The brain advertises itself over mDNS as `_voice-brain._tcp` so a satellite can discover it
automatically. Supplying the token via a systemd `EnvironmentFile` (`GABAI_VOICE_AUTH_TOKEN=…`) works
too, per the [precedence](#precedence) rules — no copy needs to live in `settings.json`.

---

## Pairing a voice device

Once the brain has a token, a fresh front end can obtain that token **over the wire** during a short,
human-approved window — no hand-copying required. The trust anchor is a person at the brain approving
a specific device (not the network). Full protocol: **[PAIRING.md](PAIRING.md)**.

**On the brain host**, open the pairing window:

```bash
gab --pair-voice-agent
```

It opens a short window, lists each device asking to pair — showing its self-reported label next to
the **authoritative observed source IP** — and issues the token only to the one you approve with a
keystroke. Trust the observed IP, not the label. (If the brain has no token yet, it tells you to run
`gabagent-install --enable-voice-host --host <LAN_IP>` first.)

**On the device**, the front end discovers the brain (mDNS or a configured host) and POSTs to the
brain's `POST /pair` endpoint, polling until you approve it, at which point it receives and stores the
token. The [reference voice front end](https://github.com/indyfive11/voice-agent) does this for you;
any conforming front end can pair with any conforming brain. See
[PAIRING.md](PAIRING.md) for the exact request/response contract.
