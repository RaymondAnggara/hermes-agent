# Phase 4 W3 — containment-(c): model-key egress proxy + secrets-out (PROTOTYPE)

Status: **LIVE as of 2026-06-25 — c1 + c2 + c3 all cut over.** The agent runs
with a placeholder model key and `base_url: http://127.0.0.1:8787`; the real
`OPENCODE_GO_API_KEY` is out of `~/.hermes/.env`. The launchd-managed supervisor
(`ai.hermes.modelproxy`) now resolves the key from **Bitwarden Secrets Manager**
(c2, project "Hermes Agent", secret id `5167b447-…`), authenticating with a
machine-account **`BWS_ACCESS_TOKEN` pinned in the macOS Keychain** (c3 bootstrap
item `hermes-bws-access-token`/`bws`) — so neither the model key nor the
bootstrap token is ever plaintext on disk or in the agent's env. Verified
end-to-end: supervisor logged "resolved model key from bitwarden", proxy healthy,
`hermes -z` returned `pong`; the Bitwarden value was hash-matched against the
live key before cutover. A **Keychain copy of the model key**
(`hermes-tsorf-model-key`/`opencode-go`) is retained as a documented break-glass
fallback (switch the plist `--backend` to `keychain`). Rollback in the runbook
below (backups: `.env.bak.*`, `config.yaml.bak.*`). STILL OPEN: the network-egress
allowlist. This directory is deploy tooling (footprint ladder: edge, not core);
nothing here imports the running agent.

A live-cutover finding (test the map, don't trust it): the opencode.ai zen/go
relay GZIPs responses regardless of Accept-Encoding. The proxy now relays
Content-Encoding + raw bytes transparently (it must NOT strip Content-Encoding
while passing compressed bytes). Regression-tested in test_phase4_w3_proxy.py.

## Why

Today the opencode-go / DeepSeek model key (`OPENCODE_GO_API_KEY`) is **plaintext
in `~/.hermes/.env`** and lives in the agent process's own environment. A
prompt-injected agent could read and exfiltrate it. The content-level egress
filter (`gateway/secret_egress.py`) is defense-in-depth only — it can't stop the
agent making an arbitrary outbound connection at the OS layer. Containment-(c)
moves the key **out of the agent's blast radius** and (separately) constrains
where the agent can talk.

## What's built (this directory)

| File | Role | Step |
|---|---|---|
| `model_key_proxy.py` | aiohttp reverse proxy on `127.0.0.1`. Drops the agent's inbound `Authorization`/`x-api-key`, injects the **real** key server-side, streams (SSE) responses back. Never logs the key. | c1 |
| `keychain_bootstrap.sh` | Store/read the key in the macOS Keychain (`security`). Present today, zero-dependency bootstrap store. | c3 |
| `supervisor.py` | Separate process that reads the key from Keychain (c3) **or** Bitwarden (c2, via `bws`), launches the proxy with the key in the **proxy's** env only. The agent is started separately with no key. | c2/c3 |

Tests: `tests/gateway/test_phase4_w3_proxy.py` (11 tests, incl. a real
end-to-end streaming proxy run against a stub upstream proving the injected key
reaches the upstream and the agent's placeholder/sneaked key does not).

Verified live on this host: `keychain_bootstrap.sh store-stdin/read/check/delete`
+ `supervisor.py --dry-run` resolve a Keychain item and report its shape without
ever printing it. (Verified with a throwaway value; host left clean.)

## Trust boundary after cutover

```
  ┌─────────────┐   no key, Bearer sk-LOCAL-PLACEHOLDER   ┌──────────────┐   Bearer <REAL key>   ┌────────────────────┐
  │  Hermes     │ ─────────────────────────────────────▶ │  proxy       │ ────────────────────▶ │ opencode.ai/zen/go │
  │  agent      │   base_url=http://127.0.0.1:8787        │ (holds key)  │                       └────────────────────┘
  └─────────────┘                                         └──────────────┘
        ▲ no model key in env / .env                            ▲ key in proxy env only, from…
                                                          ┌──────────────┐
                                                          │ supervisor   │ ── reads ──▶ Keychain (c3) or Bitwarden (c2)
                                                          └──────────────┘
```

The prompt-injectable agent never holds the real key and **cannot override** the
injected one (the proxy drops whatever Authorization the agent sends).

---

## GATED CUTOVER RUNBOOK (do NOT run without operator approval)

Each step is reversible; the gateway runs from the branch working tree, so a
restart reloads code. Keep a `.env` / `config.yaml` backup before editing
(the deployment already timestamps these).

1. **Pin the real key into the Keychain** (c3), then remove it from `.env`:
   ```bash
   # read the current value, store it, scrub the plaintext
   grep '^OPENCODE_GO_API_KEY=' ~/.hermes/.env | cut -d= -f2- \
     | bash keychain_bootstrap.sh store-stdin
   # (then edit ~/.hermes/.env: delete the OPENCODE_GO_API_KEY line)
   ```
2. **Start the supervisor + proxy** (separate process; not the agent):
   ```bash
   python supervisor.py --backend keychain --port 8787 \
       --upstream https://opencode.ai/zen/go/v1
   # health: curl -s http://127.0.0.1:8787/healthz
   ```
3. **Repoint the agent** in `~/.hermes/config.yaml`:
   ```yaml
   model:
     provider: opencode-go
     base_url: http://127.0.0.1:8787      # was https://opencode.ai/zen/go/v1
   ```
   and set the agent's `OPENCODE_GO_API_KEY=sk-LOCAL-PROXY-PLACEHOLDER` (a
   non-empty placeholder so the provider sends *an* Authorization header for the
   proxy to replace; the proxy ignores its value).
4. **Restart the gateway** (`hermes gateway restart` — brief Discord drop) and
   send one `#tldr`/`#general` message to confirm a model round-trip works
   through the proxy. Watch `hermes logs --follow`.
5. **Mirror** the config change to `deploy/tsorf-discord/channel-modes.example.yaml`
   / this dir and commit.

**Rollback:** restore `base_url` to the upstream + put the key back in `.env`
from the Keychain (`keychain_bootstrap.sh read`), restart the gateway.

**What could break (verify live):** the provider may send extra auth headers, or
opencode-go may require a header the proxy drops; the per-model `max_tokens`
logic lives in the provider profile (client-side), so it is unaffected. The
proxy adds one loopback hop (~sub-ms). `container_persistent` does **not** apply
(the proxy is host-side), but if the agent runs inside the docker backend its
`127.0.0.1` is the container's loopback — then the proxy must bind a
host-reachable address and the docker network must allow it (see egress below).

---

## c2 — secrets into Bitwarden Secrets Manager (design; `bws` now installed)

`bws` 2.1.0 is installed (`/usr/local/bin/bws`). The GitHub PAT already lives in
Bitwarden SM (project **"Agent Hermes"**, secret `GITHUB_TOKEN`). Plan:

- Add `OPENCODE_GO_API_KEY` as a secret in the same project; note its secret-id.
- The **supervisor** reads it: `supervisor.py --backend bitwarden
  --bitwarden-secret-id <id>`. Requires `BWS_ACCESS_TOKEN` (the ONE bootstrap
  token) in the **supervisor's** env only.
- **Bootstrap-token hardening:** keep `BWS_ACCESS_TOKEN` in the Keychain (not
  plaintext), scoped to a machine account with **read-only** access to just the
  "Agent Hermes" project. The supervisor reads it via `security` at launch. So
  the agent never holds the bootstrap token *or* the raw secrets.
- This generalizes the same pattern to the GitHub PAT and (Phase 5) exchange
  keys: one supervisor, secrets injected per-process, agent sees none.

## Network egress allowlist — the remaining honest gap

The content filter + key proxy do **not** stop the agent opening arbitrary
outbound sockets. A real default-deny egress control is a network/OS-layer job.
Options for this macOS-native deployment, least → most isolating:

1. **Userspace allowlist proxy (cheapest, partial).** Force all agent HTTP(S)
   through an allowlisting forward proxy (`HTTPS_PROXY`) that permits only:
   Discord API, the model proxy (`127.0.0.1:8787`), and declared research
   domains (Tavily). Defeatable by an agent that ignores `HTTPS_PROXY`, so this
   is *policy*, not enforcement.
2. **Run the agent in the docker backend with a locked network (real enforce).**
   `--network` restricted to an internal bridge; an allowlisting egress proxy is
   the only route off the bridge (`--network=none` + a sidecar, or a user-defined
   network + iptables in the proxy container). This is the genuine default-deny
   and the recommended target. Cost: re-hosting the orchestrator in a container
   (the "full re-host" the prompt flags as the biggest risk), which also solves
   the orchestrator-secret-isolation problem.
3. **Host packet filter (`pf`).** A macOS `pf` anchor that default-denies
   outbound for the gateway's user/uid except the allowlist. Enforced at the OS,
   but brittle to maintain and macOS-specific.

Recommendation: ship the proxy + Keychain/Bitwarden now (this dir), and treat
the **docker-network default-deny (option 2)** as the next gated deliverable —
it's the step that turns "security is infrastructure" from mostly-true into
true for network egress. Tracked as the skipped test
`test_network_egress_is_default_deny` in `tests/gateway/test_phase4_redteam.py`.

## Network default-deny — BUILT (option 2), gated cutover (see `network/`)

Option 2 is now built under `network/` (edge tooling; nothing imports core):

| File | Role |
|---|---|
| `docker-compose.locked.yml` | Two networks: `hermes-internal` (**`internal: true`** — no internet route) holds the agent ONLY; `hermes-egress` (bridge) is touched ONLY by the proxy. Agent runs gateway-only with `HTTP(S)_PROXY → egress-proxy`. Replaces the shipped compose's `network_mode: host`. |
| `egress-proxy.Dockerfile` + `tinyproxy.conf` + `allowlist.filter` | Alpine+tinyproxy forward proxy, `FilterDefaultDeny On`. The sole route off-box; default-denies every destination except the closed allowlist. |
| `config.locked.overlay.yaml` | Config keys to apply AT cutover: `model.base_url → host.docker.internal:8787`, `terminal.backend → local`, `web.search_backend → tavily`. |
| `validate_egress.sh` | Proves isolation with ONLY the proxy + a throwaway client (no agent, no prod impact). |

**Enforcement is at L3, not L7.** The agent is on an `internal: true` network with no
route to the internet, so a direct socket to any external IP is dropped regardless of
proxy settings — `validate_egress.sh` tests this directly (direct-no-proxy → no route).
The proxy then default-denies *which* hosts it will relay (L7). Closed allowlist:
`host.docker.internal` (model proxy), `*.discord.com/.gg/.discordapp.com/.discordapp.net`
(bot), `*.tavily.com` (web search+extract, server-side). Everything else fails closed —
so the tool inventory does not have to be trusted.

**Why these were verified, not assumed:** discord.py routes both REST and the gateway WS
through `HTTPS_PROXY` via `resolve_proxy_url`/`proxy_kwargs_for_bot`
(`gateway/platforms/base.py:348,382`) — no adapter change needed. Web search defaults to
`ddgs`/DuckDuckGo whose Rust client may ignore proxy env, so the overlay switches search
to Tavily (httpx, proxy-honoring) to keep the allowlist closed and proxy-clean.

### GATED cutover runbook (do NOT run without operator approval)
1. `docker build -t hermes-agent:locked .` (repo root). Back up `config.yaml`/`.env`.
2. Apply `config.locked.overlay.yaml`; add the `#coding` scratch repo to `agent.volumes`.
3. `launchctl stop ai.hermes.gateway` (native bot off).
4. `cd network && HERMES_UID=$(id -u) HERMES_GID=$(id -g) docker compose -f docker-compose.locked.yml up -d`.
5. Verify live: Discord connects through the proxy; one msg per #tldr/#learning/#investment
   round-trips; model path OK; a non-allowlisted egress attempt is dropped (live red-team).
6. Mirror + commit.
**Rollback (<1 min):** `docker compose … down` → restore config/.env backups →
`launchctl enable gui/$(id -u)/ai.hermes.gateway` (it is persistently DISABLED at
cutover so a reboot can't start a conflicting native bot) → `hermes gateway start`.

**Top risk to confirm live:** the bot-connects-through-proxy step can only be fully proven
with the real (or a throwaway) bot token, since two live connections on the prod token
conflict — it is the first thing checked at cutover with rollback armed.

## Phase 5b secrets-hardening — platform tokens out of plaintext `.env` (LIVE 2026-06-27)

`DISCORD_BOT_TOKEN` and `TAVILY_API_KEY` were removed from the mounted `~/.hermes/.env`
(which the agent can `cat` at `/opt/data/.env`) and are now stored in Bitwarden
("Hermes Agent" project) and **injected into the locked container's process env at
launch** by `network/launch_locked.sh`. The Bitwarden **bootstrap** (`BWS_ACCESS_TOKEN`,
read from the Keychain item `hermes-bws-access-token/bws`) stays on the host launcher and
**never enters the container** — putting it inside the prompt-injectable agent would
expose the whole vault, including the model key W3 removed.

**Honest residual:** discord.py and the Tavily client need their tokens in-process, so the
agent can still read its own `/proc/self/environ`. This removes plaintext-at-rest in the
mounted volume and keeps the bootstrap off the container; it does NOT hide a websocket-auth
token from the process that opens the socket. The egress value-redaction layer
(`gateway/secret_egress.py`) scrubs the literal values from outbound Discord messages.

**DEPLOY/RESTART MODEL CHANGED for the locked stack:** bring the stack up/restart it with
`network/launch_locked.sh` (it fetches the tokens from Bitwarden and injects them), **not**
a bare `docker compose up`. The compose interpolates `${DISCORD_BOT_TOKEN:?}` /
`${TAVILY_API_KEY:?}` — the `:?` makes a tokenless `docker compose up` **fail closed**
rather than start a silently-broken bot. `./launch_locked.sh --dry-run` resolves+confirms
the tokens (by length) without touching docker. Code change → rebuild image then
`launch_locked.sh`; config change → `launch_locked.sh` (recreates the `agent` service,
leaves `egress-proxy` up). Tests: `tests/gateway/test_phase5b_secrets_hardening.py` (9).

**Rollback (<1 min):** the simplest correct revert is just to **keep using
`launch_locked.sh`** — it always works. To go fully back to the plaintext-`.env` model:
(1) `git checkout` the compose `environment:` additions (remove the two `${..:?}` lines —
compose interpolation reads the launcher's SHELL env, not the agent-runtime `~/.hermes/.env`,
so the `:?` is NOT satisfied just by restoring `.env`); (2) restore
`~/.hermes/.env.bak.20260627-101747` (re-adds the two tokens as plaintext); (3) bare
`docker compose -f network/docker-compose.locked.yml up -d` (the agent reads the tokens from
the mounted `.env` at runtime as before). Bitwarden keeps its copies either way.
