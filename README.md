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

An **optional reconciler** can push this node's configuration into *another*
mailcow node through that node's REST API (see [Reconciler](#reconciler-optional)).
With no target configured the sidecar stays strictly read-only.

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
| `SIDECAR_API_TOKEN` | *(empty)* | If set, clients must send `Authorization: Bearer <token>`. |
| `SIDECAR_MAILCOW_DIR` (or `MAILCOW_DIR`) | auto | Directory containing `mailcow.conf`. Auto-detects `/opt/mailcow-dockerized`, `/opt/mailcow`, `/srv/mailcow-dockerized`. |
| `SIDECAR_COMPOSE_PROJECT` | from `mailcow.conf` | Compose project name (default `mailcowdockerized`). |
| `SIDECAR_DOCKER_BIN` | `docker` | Docker CLI binary. |
| `SIDECAR_REQUEST_TIMEOUT` | `30` | Seconds per `docker exec`. |
| `SIDECAR_CACHE_TTL` | `30` | Seconds to cache scraped data (`0` disables). |
| `SIDECAR_TARGET_URL` | *(empty)* | Reconciler target, e.g. `https://mail.example.org`. Enables the write endpoints. |
| `SIDECAR_TARGET_API_KEY` | *(empty)* | **`rw`** API key on the target. |
| `SIDECAR_TARGET_VERIFY_TLS` | `true` | Set `false` for self-signed certs on the target. |
| `SIDECAR_TARGET_TIMEOUT` | `30` | Seconds for target API calls. |
| `SIDECAR_PEER_TOKEN` | *(empty)* | Default token for peer sidecars (their `SIDECAR_API_TOKEN`). |
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

Open (no auth): `GET /`, `GET /health`.

Everything under `/api/v1` requires the bearer token when `SIDECAR_API_TOKEN`
is set.

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
| GET | `/api/v1/export` | Everything in one bundle (domains, DKIM, DNS, mailboxes, aliases). |
| GET | `/api/v1/reconcile/target` | Target reachability + version. |
| POST | `/api/v1/reconcile/plan` | Diff desired state against the target (no writes). |
| POST | `/api/v1/reconcile/apply` | Execute that diff against the target. |
| POST | `/api/v1/replicate/peer` | Probe another sidecar (reachability + identity). |
| POST | `/api/v1/replicate/preview` | Ask a peer what it would change (no writes). |
| POST | `/api/v1/replicate/push` | Scrape this node and push it to a peer sidecar. |

Examples:

```bash
curl -s http://127.0.0.1:8800/api/v1/dkim/example.org | jq
curl -s -H "Authorization: Bearer $TOKEN" http://127.0.0.1:8800/api/v1/dns/example.org | jq
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

## Reconciler (optional)

Pushes configuration into another mailcow node so a freshly set up instance can
be provisioned, or so several nodes converge on the same domains.

**Source:** either the local node (scraped read-only from its containers) or a
supplied `ExportBundle` JSON — e.g. a row from your own database, or the output
of `GET /api/v1/export`.

**Target:** a mailcow node reached over its REST API. It needs an API key with
`access='rw'` whose `allow_from` covers this host's source IP (or
`skip_ip_check=1`). See `SIDECAR_TARGET_*` above.

```bash
# Dry-run: what would change on the target?
curl -s -X POST http://127.0.0.1:8800/api/v1/reconcile/plan \
  -H 'Content-Type: application/json' -d '{}' | jq

# Apply it (also from a stored bundle):
curl -s -X POST http://127.0.0.1:8800/api/v1/reconcile/apply \
  -H 'Content-Type: application/json' \
  -d '{"bundle": { ... }, "prune": false, "include_mailboxes": true}' | jq
```

Or from the CLI (no HTTP server needed):

```bash
python -m app.reconcile --target-url https://mail.example.org \
  --source local --apply            # reads this node, writes the target
python -m app.reconcile --target-url https://mail.example.org \
  --source bundle --bundle export.json --prune --apply
```

Plans never contain DKIM private keys (they are re-read at apply time), so plan
output is safe to log. `GET /api/v1/reconcile/target` is a quick reachability
check.

For recurring syncs, `deploy/mailcow-reconcile.service` + `.timer` run the CLI
every 15 minutes against `SIDECAR_TARGET_URL`.

### What is reconciled

domains · DKIM private keys (`dkim_import`, so signatures match across nodes) ·
alias domains · aliases · mailboxes (opt-in).

### Limitations to know

- **Mailbox passwords cannot be synced** — mailcow stores only a hash. Created
  mailboxes get a random password plus `force_pw_update=1`; manage them only
  with `include_mailboxes: true`.
- **Deletes only happen with `prune: true`**, and alias deletion is limited to
  domains you manage.
- **mailcow is single-master.** This converges *configuration*; it is not
  replication of mail data (`vmail-vol-1`). For host-level HA use mailcow's
  cold-standby script instead.
- **Mail data and DNS targets are per-node.** MX/autodiscover/SRV records point
  at each node's own `MAILCOW_HOSTNAME`; only the domain/DKIM/mailbox layer is
  node-independent.

## Self-replication (node → node)

Run this sidecar on **both** nodes. Paste Node 2's URL into Node 1's API, and
Node 1 ships its whole configuration to Node 2, which provisions its own
containers.

```
┌─ Node 1 · full mailcow ─────────────────┐        ┌─ Node 2 · fresh mailcow ────────────────┐
│  sidecar :8800                          │        │  sidecar :8800                          │
│                                         │        │                                         │
│  scrape redis+mysql ──► ExportBundle    │ POST   │  /api/v1/reconcile/apply                │
│  POST /api/v1/replicate/push ───────────┼───────►│    └─► Node 2's own mailcow REST API    │
│                                         │ Bearer │         (SIDECAR_TARGET_URL)             │
└─────────────────────────────────────────┘        └─────────────────────────────────────────┘
```

The receiver is just the reconciler you already have — Node 2's "target" is its
**own** mailcow.

### One-time setup on Node 2 (the empty instance)

1. Install mailcow + this sidecar and let it come up.
2. In Node 2's mailcow UI create an API key with **read-write** access
   (`Configuration → Access → Edit administrator details → API`). Set
   `allow_from` to the sidecar host's source IP, or tick *skip IP check*.
3. Point Node 2's sidecar at its own mailcow:

   ```ini
   SIDECAR_TARGET_URL=https://127.0.0.1      # or the mailcow hostname
   SIDECAR_TARGET_API_KEY=<the rw key>
   SIDECAR_TARGET_VERIFY_TLS=false           # if using a self-signed cert
   SIDECAR_API_TOKEN=<optional: require a token from Node 1>
   ```

### Push from Node 1

```bash
# 1. Check you're talking to the right peer.
curl -s -X POST http://127.0.0.1:8800/api/v1/replicate/peer \
  -H 'Content-Type: application/json' \
  -d '{"peer_url": "https://node2:8800", "peer_token": "node2-token"}' | jq

# 2. Dry-run first: the peer diffs your bundle against its own state, writes nothing.
curl -s -X POST http://127.0.0.1:8800/api/v1/replicate/preview \
  -H 'Content-Type: application/json' \
  -d '{"peer_url": "https://node2:8800", "peer_token": "node2-token"}' | jq '.counts'

# 3. Push it.
curl -s -X POST http://127.0.0.1:8800/api/v1/replicate/push \
  -H 'Content-Type: application/json' \
  -d '{"peer_url": "https://node2:8800", "peer_token": "node2-token",
       "include_mailboxes": true, "prune": false}' | jq '.peer_result'
```

`preview` sends your bundle to the peer, so the *peer* computes the diff — only it
knows its own state. Use it to see exactly what a push would do before pulling
the trigger.

### Notes

- **Ordering is handled**: domains are created before DKIM imports, alias
  domains, aliases and mailboxes, so a fresh node provisions cleanly.
- **The wire carries DKIM private keys** (that's how both nodes sign
  identically). Use HTTPS and always set `SIDECAR_API_TOKEN` on the peer.
- **Node 2 stays empty of mail** — this replicates configuration only. Copy
  `vmail-vol-1` separately if you want the actual messages.
- Node 2 can be re-pushed to at any time; the diff means it's idempotent, and
  the second push is a no-op.
- The receiver needs no Docker access for the write path — it only needs its own
  mailcow API key.

## Tests

```bash
pip install -r requirements-dev.txt
python -m pytest -q
```

The suite runs against a stub `docker` binary (`tests/stub_docker.py`) and a
temporary `mailcow.conf`, so no real mailcow install is required.

## Security notes

- The API returns DKIM **private keys**. Bind to localhost and/or set
  `SIDECAR_API_TOKEN`, and put it behind a firewall if exposed.
- Passwords are never put on the command line: `REDISPASS` is passed via
  `REDISCLI_AUTH` and `DBPASS` via `MYSQL_PWD`, so they don't appear in the
  container process list.
