# mailcow scraper sidecar

A small, **read-only** FastAPI service that scrapes domain information out of a
running [mailcow: dockerized](https://github.com/mailcow/mailcow-dockerized)
installation and serves it as JSON.

It reaches *into the running containers* over `docker exec`:

| Data | Source | Notes |
| --- | --- | --- |
| DKIM public/private keys, selector | Redis (`redis-mailcow`) hashes `DKIM_PUB_KEYS`, `DKIM_SELECTORS`, `DKIM_PRIV_KEYS` | Private keys are always available here, unlike the REST API |
| Domains, mailboxes, aliases, alias domains | MariaDB (`mysql-mailcow`, database from `mailcow.conf`) | |
| DNS records (MX, SPF, DKIM, DMARC, autodiscover/autoconfig, SRV) | computed | mailcow does not store them; this mirrors its UI logic |

It never writes to Redis/MariaDB and keeps no database of its own. The only
state is an optional in-memory TTL cache (default 30s, set `SIDECAR_CACHE_TTL=0`
to disable) to avoid hammering the containers.

A **single replication route** (`POST /api/v1/replicate`) can push this node's
configuration into another mailcow node, which applies it to its own containers
(see [Replication](#replication-node--node)). With no write target configured
the sidecar stays strictly read-only.

## Why not just use the mailcow REST API?

The official API (`GET /api/v1/get/dkim/{domain}`) returns `privkey: ""` unless
you set `SHOW_DKIM_PRIV_KEYS=1` in `mailcow.conf`. Reading Redis directly gives
you the private keys regardless.

## Requirements

- The host running mailcow, with root (or docker-group) access.
- The `docker` CLI on `PATH`.
- mailcow containers **running** (`redis-mailcow`, `mysql-mailcow`).
- Python 3.11+.

## Install

```bash
cd /opt && git clone <this-repo> mailcow-scraper-sidecar
cd mailcow-scraper-sidecar

python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt

cp .env.example .env   # then edit if needed
```

## Configure

All settings are environment variables (also read from `.env`):

| Variable | Default | Purpose |
| --- | --- | --- |
| `SIDECAR_HOST` | `127.0.0.1` | Bind address. Keep on localhost; the API exposes private keys. |
| `SIDECAR_PORT` | `8800` | Bind port. |
| `SIDECAR_MAILCOW_DIR` (or `MAILCOW_DIR`) | auto | Directory containing `mailcow.conf`. Auto-detects `/opt/mailcow-dockerized`, `/opt/mailcow`, `/srv/mailcow-dockerized`. |
| `SIDECAR_COMPOSE_PROJECT` | from `mailcow.conf` | Compose project name (default `mailcowdockerized`). |
| `SIDECAR_DOCKER_BIN` | `docker` | Docker CLI binary. |
| `SIDECAR_REQUEST_TIMEOUT` | `30` | Seconds per `docker exec`. |
| `SIDECAR_CACHE_TTL` | `30` | Seconds to cache scraped data (`0` disables). |
| `SIDECAR_TARGET_URL` | *auto* | Override the write target. Defaults to this node's own mailcow (`https://127.0.0.1:<HTTPS_PORT>`). |
| `SIDECAR_TARGET_API_KEY` | *auto* | Override the `rw` API key. Defaults to an existing key, or one the sidecar creates. |
| `SIDECAR_TARGET_VERIFY_TLS` | `true` | TLS verification for an explicit `SIDECAR_TARGET_URL`. A derived loopback target always skips it. |
| `SIDECAR_TARGET_TIMEOUT` | `30` | Seconds for target API calls. |
| `SIDECAR_AUTO_API_KEY` | `true` | Create an `rw` API key in this node's mailcow when none exists (zero-config receive). |
| `SIDECAR_PEER_VERIFY_TLS` | `true` | Verify TLS when talking to a peer sidecar. |
| `SIDECAR_PEER_TIMEOUT` | `120` | Seconds for peer push calls. |

`MAILCOW_HOSTNAME`, `DBNAME`, `DBUSER`, `DBPASS`, `REDISPASS`, `HTTPS_PORT`,
`SPLIT_DKIM_255` and `COMPOSE_PROJECT_NAME` are all read from `mailcow.conf`.

## Run

```bash
. .venv/bin/activate
uvicorn app.main:app --host 127.0.0.1 --port 8800
```

Or as a service (see `deploy/mailcow-scraper-sidecar.service`):

```bash
sudo cp deploy/mailcow-scraper-sidecar.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now mailcow-scraper-sidecar
```

Interactive docs are at `http://127.0.0.1:8800/docs`.

## Endpoints

**There is no authentication.** Every endpoint, including the ones that return
DKIM private keys and the write/replicate endpoints, is open to anyone who can
reach the port. Keep it bound to localhost (the default) or firewall it.

| Method | Path | Description |
| --- | --- | --- |
| GET | `/api/v1/status` | Resolved config + container reachability. |
| POST | `/api/v1/refresh` | Clear the in-memory cache. |
| GET | `/api/v1/domains?include_dns=true` | All domains (+ DKIM + DNS). |
| GET | `/api/v1/domains/{domain}` | One domain. |
| GET | `/api/v1/alias-domains` | Alias domain → target mappings. |
| GET | `/api/v1/dkim` | All DKIM keys (public **and private**). |
| GET | `/api/v1/dkim/{domain}` | One DKIM key. |
| GET | `/api/v1/dns?include_srv=true` | Generated DNS records, keyed by domain. |
| GET | `/api/v1/dns/{domain}` | DNS records for one domain. |
| GET | `/api/v1/mailboxes?domain=` | Mailboxes. |
| GET | `/api/v1/aliases?domain=` | Aliases. |
| GET | `/api/v1/export` | Everything in one bundle (domains, DKIM, DNS, mailboxes, aliases, relayhosts, transports, domain admins). |
| POST | `/api/v1/replicate` | Push to a peer (`{"peer": "10.0.0.2"}`) or apply a bundle locally (`{"bundle": {...}}`). |

Examples:

```bash
curl -s http://127.0.0.1:8800/api/v1/dkim/example.org | jq
curl -s http://127.0.0.1:8800/api/v1/dns/example.org | jq
curl -s "http://127.0.0.1:8800/api/v1/domains?include_dns=false" | jq '.[].domain'
```

DKIM key response:

```json
{
  "domain": "example.org",
  "selector": "dkim",
  "length": "2048",
  "public_key": "MIIBIjANBgkqhkiG9w0BAQEF...",
  "private_key": "-----BEGIN PRIVATE KEY-----\n...\n-----END PRIVATE KEY-----\n",
  "dkim_txt": "v=DKIM1;k=rsa;t=s;s=email;p=MIIBIjANBgkqhkiG9w0BAQEF..."
}
```

## Replication (node → node)

One route, two directions. Run this sidecar on **both** nodes.

```
┌─ Node 1 · full mailcow ────────────────┐        ┌─ Node 2 · fresh mailcow ──────────────┐
│  sidecar :8800                         │        │  sidecar :8800                        │
│                                        │        │                                       │
│  scrape redis+mysql ──► ExportBundle   │ POST   │  POST /api/v1/replicate               │
│  POST /api/v1/replicate ───────────────┼───────►│    {"bundle": ...}                    │
│    {"peer": "10.0.0.2"}                │        │    └─► Node 2's own mailcow REST API  │
└────────────────────────────────────────┘        │        (SIDECAR_TARGET_URL)           │
                                                  └───────────────────────────────────────┘
```

- **send** — `{"peer": "10.0.0.2"}`: scrape this node, then POST the bundle to
  `{peer}/api/v1/replicate`.
- **receive** — `{"bundle": {...}}`: apply that bundle to this node's own mailcow.

`peer` takes a bare IP/host (`10.0.0.2`, `10.0.0.2:9000` — `http://` and port
`8800` are assumed) or a full URL like `https://mail.example.org`.

### Node 2 (the empty instance) — zero config

1. Install mailcow + this sidecar and let it come up.
2. That's it — **no configuration needed**.

Node 2 resolves its own write target automatically: it derives the URL from
`mailcow.conf` (`https://127.0.0.1:<HTTPS_PORT>`, TLS verification off since the
cert is issued for `MAILCOW_HOSTNAME`), reuses any active `rw` API key already in
mailcow, and — if there is none — creates one for itself
(`SIDECAR_AUTO_API_KEY=true`, the default). Check what it settled on:

```bash
curl -s http://127.0.0.1:8800/api/v1/status | jq .write_target
# {"url": "https://127.0.0.1:443", "source": "existing-key"}
```

`source` is `configured | existing-key | created-key | auto-create | unavailable`.
Set `SIDECAR_AUTO_API_KEY=false` if you'd rather create the key yourself; then you
must also set `SIDECAR_TARGET_API_KEY`. Node 1 never needs any of this — it only
scrapes and sends.

### Push from Node 1

```bash
curl -s -X POST http://127.0.0.1:8800/api/v1/replicate \
  -H 'Content-Type: application/json' \
  -d '{"peer": "10.0.0.2", "include_mailboxes": true, "include_domain_admins": true}' | jq
```

```json
{"mode": "send", "peer": "http://10.0.0.2:8800",
 "domains": 2, "mailboxes": 1, "aliases": 1, "applied": 9, "failed": 0, "errors": []}
```

`domains`/`mailboxes`/`aliases` are what Node 1 sent; `applied`/`failed`/`errors`
are what Node 2 did.

### What gets replicated

| Replicated | Fidelity |
| --- | --- |
| domains, alias domains, aliases | exactly |
| DKIM keys | **private keys imported**, so both nodes sign identically |
| relayhosts, transports | **real SMTP credentials** (mailcow keeps these in the clear) |
| mailboxes | account/quota/active only — **new random password** + `force_pw_update` |
| domain admins | account + domain grants only — **new random password** |

Mailboxes and domain admins are opt-in (`include_mailboxes` /
`include_domain_admins`), because mailcow stores their passwords as one-way
hashes that no API can read back.

**Not replicable through the mailcow API at all** — these would need a raw DB
copy, which is what mailcow's own `backup_and_restore.sh` / cold-standby already
do:

- the main `admin` account (no API to create one; password is hashed)
- mailbox, app-password and sync-job passwords (hashed, or never returned)
- API keys (no create endpoint)
- TFA secrets (device-bound — copying them would break logins)
- OAuth2 client secrets (`add/oauth2-client` regenerates the secret)
- mail, sieve filters, SOGo data, quarantine → volume data, not config

### Notes

- **Ordering is handled**: domains are created before DKIM imports, alias
  domains, aliases, mailboxes and domain admins.
- **Idempotent**: the receiver diffs against its own state first, so re-pushing
  is a no-op and a half-finished push can safely be repeated.
- **`prune: true` is the only destructive option** — it deletes on the peer
  whatever is absent locally. Leave it off until you trust the sync.
- **The wire carries DKIM private keys and SMTP credentials.** Use HTTPS or an
  SSH tunnel — there is no authentication on this endpoint, so never expose it to
  the internet.
- **Configuration only** — Node 2 gets no mail. Copy `vmail-vol-1` separately if
  you want the actual messages.
- **DNS targets are per-node** — MX/autodiscover/SRV point at each node's own
  `MAILCOW_HOSTNAME`.
- **mailcow is single-master** — for host-level HA use mailcow's cold-standby
  script instead.

### CLI (optional, no HTTP needed)

Node 1 can also write straight into a target's mailcow, skipping its sidecar. With
no `--target-*` it resolves the local node's own target, same as the API:

```bash
python -m app.reconcile --source local --apply          # local mailcow
python -m app.reconcile --target-url https://mail.example.org \
  --target-api-key <rw key> --apply                     # a remote node
```

Add `--include-mailboxes` / `--include-domain-admins` / `--prune` as needed.

`deploy/mailcow-reconcile.service` + `.timer` run that every 15 minutes.

## Tests

```bash
pip install -r requirements-dev.txt
python -m pytest -q
```

The suite runs against a stub `docker` binary (`tests/stub_docker.py`) and a
temporary `mailcow.conf`, so no real mailcow install is required.

## Security notes

- **The API has no authentication.** It returns DKIM **private keys** and can
  write to mailcow. It binds to localhost by default — keep it that way, tunnel
  it over SSH, or firewall the port. Do not expose it to the internet as-is.
- Passwords are never put on the command line: `REDISPASS` is passed via
  `REDISCLI_AUTH` and `DBPASS` via `MYSQL_PWD`, so they don't appear in the
  container process list.
