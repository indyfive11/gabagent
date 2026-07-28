# Voice Brain Pairing

An extension to the [Voice Brain Protocol](docs/VOICE_PROTOCOL.md) that lets a fresh voice front end
obtain the brain's bearer token **over the wire**, during a short human-authorized window, instead of a
human copying the token into the device by hand.

> **Ownership is not yet finalized.** The parent Voice Brain Protocol is owned by the voice-agent
> project; where this pairing spec ultimately lives is the maintainer's direct call. This document is
> the **provisional** home so implementers have a contract to build against; it links the parent spec
> above so the two do not fork. The **client-facing `POST /pair` contract below is front-end-agnostic** —
> any conforming front end can pair with any conforming brain. The operator side (`--pair-voice-agent`,
> the `/pair/*` admin routes) is **brain-private** and NOT part of the agnostic contract.

## Trust model (read this first)

The trust anchor is a **human at the brain** who opens a window *and approves a specific device*. It is
**not** the network. Transport is cleartext HTTP: pairing removes the **typing**, not the on-wire
exposure — an on-path sniffer still reads the token here and on every later call (that needs TLS, out of
scope). What pairing does defend:

- **Guessing** (switched LAN, no sniff): retrieval also requires a **server-generated** high-entropy
  `claim_secret`, so a weak client-chosen `client_id` cannot be guessed into a token.
- **Off-path spoofing**: a candidate is pinned to the source IP that registered it; a different IP
  presenting the same `client_id` is rejected.
- **Race-to-grab**: only the one candidate a human **approves** becomes retrievable.

## The agnostic contract — `POST /pair`

Unauthenticated (the front end has no token yet; the operator-opened window + approval is the gate).
Body is JSON; all fields optional except `client_id`:

| field | notes |
|-------|-------|
| `client_id` | **required**, the client's stable idempotency key. MUST be ≥128-bit random and persisted **before the first POST**. MUST NOT be derived from `room_id`/hostname/MAC. Server enforces a length floor (≥20 chars). |
| `claim_secret` | the server-issued secret, echoed back once the client has it (see flow). |
| `label` | human-friendly device name for the operator display. **Untrusted / display-only** (server sanitizes + clamps it). |
| `room_id` | optional room label. Display-only; does not route. |

Responses:

| status | body | meaning / client action |
|--------|------|-------------------------|
| `202` | `{"status":"pending"\|"accepted","claim_secret":"…"}` | Registered / awaiting approval. Store `claim_secret`; keep polling. |
| `200` | `{"auth_token":"…","token_scheme":"bearer"}` | Approved → token issued. Store it; use `Authorization: Bearer`. **Stop.** MUST reject an empty/absent `auth_token` and any unknown `token_scheme`. |
| `409` | `{"error":"no_pairing_window_open"\|"client_id_registered_from_different_peer"}` | Retryable within a bounded deadline (wait for the operator to open/approve). |
| `403` | `{"error":"bad_claim_secret"}` | The presented secret doesn't match — terminal for this client. |
| `400` | `{"error":"…"}` | Malformed request (e.g. `client_id` too short) — terminal. |
| `501` | `{"error":"pairing_unsupported"}` | The brain has **no auth configured** → nothing to hand out (misconfigured pairing-capable brain). Terminal — do NOT store an empty token. |
| `404` | — | Route absent → the brain predates pairing. Terminal; distinct from `409`. |

### Client flow (headless — POST + poll only, no TTY)

1. Discover the brain (mDNS `_voice-brain._tcp`, existing) or use a configured host.
2. `POST /pair {client_id, label?, room_id?}` — **secretless** first.
3. On `202`, capture `claim_secret`; thereafter always include it: `POST /pair {client_id, claim_secret}`.
4. Poll `202` → `200`. A dropped `202` recovers (secretless re-POST re-returns the same secret); a
   dropped `200` recovers (replayable until the claim TTL) — both from the **same source IP**.
5. Persist the token; use `Authorization: Bearer` on all subsequent Voice Brain Protocol calls. A later
   `401` on a normal call ⇒ the token rotated ⇒ re-pair.

## The operator side (brain-private — `gab --pair-voice-agent`)

Run on the brain host. It opens the window, lists each device that asks to pair — its **untrusted
self-reported label** next to the **authoritative observed source IP** — and issues the token only to
the one the operator approves with a keystroke. A prerequisite: the brain must hold a bearer token; the
voice-host installer (`gabagent-install --enable-voice-host`) mints one **before** the brain starts, so
an unprovisioned brain answers `POST /pair` with `501` rather than handing out an empty secret.

## Where the brain's bearer token comes from (config precedence)

The token the brain enforces and hands out is `voice_auth_token`, resolved by the standard config
precedence: **CLI/override > env (`GABAI_VOICE_AUTH_TOKEN`) > `settings.json` > default (empty)**. So a
deployment MAY supply the token purely via the environment — e.g. a systemd `EnvironmentFile` — and the
brain honors it; no copy needs to live in `settings.json`. A token present **only** in the env is
deliberately **not** written back into `settings.json` on a later config save, so the plaintext file
never accumulates a second copy of a secret the operator chose to keep in the environment. Either source
satisfies the "brain must hold a bearer token" prerequisite above; with **no** effective token from any
source, auth is off and `POST /pair` returns `501`. (An empty `GABAI_VOICE_AUTH_TOKEN=` is treated as
*unset*, not as an empty token, so a stray blank export can't silently disable a configured secret.)
