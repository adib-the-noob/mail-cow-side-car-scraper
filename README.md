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
| GET | `/api/v1/export` | Everything in one bundle. |

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
