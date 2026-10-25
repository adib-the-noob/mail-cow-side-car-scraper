"""Mailcow data access: Redis (DKIM) and MariaDB (domains/mailboxes/aliases).

All access goes through ``docker exec`` on the running mailcow containers:

* DKIM keys live in Redis hashes ``DKIM_PUB_KEYS`` / ``DKIM_SELECTORS`` /
  ``DKIM_PRIV_KEYS`` (see mailcow's ``functions.dkim.inc.php``).
* Domains, mailboxes and aliases live in the ``mailcow`` MariaDB database.

Passwords are passed via environment variables (``REDISCLI_AUTH`` /
``MYSQL_PWD``) rather than argv so they do not show up in the container's
process list.
"""

from __future__ import annotations

import json

from .cache import TTLCache
from .config import MYSQL_SERVICE, REDIS_SERVICE, MailcowConfig
from .docker_exec import DockerClient, DockerError
from .dkim import build_dkim_txt, infer_key_length
from .models import DkimKey

# Redis hashes used by mailcow for DKIM material.
REDIS_DKIM_PUB = "DKIM_PUB_KEYS"
REDIS_DKIM_SELECTORS = "DKIM_SELECTORS"
REDIS_DKIM_PRIV = "DKIM_PRIV_KEYS"

# Private keys are multi-line PEM blobs, so line-based HGETALL parsing would
# break. Round-tripping through Lua/cjson gives us a single, safely-escaped
# JSON line and works on every Redis version mailcow ships.
_HGETALL_LUA = "local r = redis.call('HGETALL', KEYS[1]); return cjson.encode(r)"

# Columns we scrape. Kept as explicit lists so the JSON keys stay stable.
DOMAIN_COLUMNS = (
    "domain",
    "description",
    "aliases",
    "mailboxes",
    "defquota",
    "maxquota",
    "quota",
    "relayhost",
    "backupmx",
    "gal",
    "relay_all_recipients",
    "relay_unknown_only",
    "active",
    "created",
    "modified",
)
MAILBOX_COLUMNS = (
    "username",
    "name",
    "domain",
    "local_part",
    "quota",
    "active",
    "authsource",
    "created",
    "modified",
)
ALIAS_COLUMNS = ("id", "address", "goto", "domain", "active")
ALIAS_DOMAIN_COLUMNS = ("alias_domain", "target_domain", "active")


def _json_array_query(table: str, columns: tuple[str, ...]) -> str:
    pairs = ", ".join(f"'{column}', `{column}`" for column in columns)
    return (
        f"SELECT COALESCE(JSON_ARRAYAGG(JSON_OBJECT({pairs})), JSON_ARRAY()) "
        f"FROM `{table}`"
    )


class MailcowClient:
    def __init__(self, config: MailcowConfig, docker: DockerClient, cache: TTLCache) -> None:
        self.config = config
        self.docker = docker
        self.cache = cache

    # ------------------------------------------------------------------ low level

    def redis_hgetall(self, hash_name: str) -> dict[str, str]:
        output = self.docker.exec(
            REDIS_SERVICE,
            ["redis-cli", "--no-auth-warning", "EVAL", _HGETALL_LUA, "1", hash_name],
            env={"REDISCLI_AUTH": self.config.redispass},
        )
        text = output.strip()
        if not text or text in {"{}", "[]"}:
            return {}
        try:
            data = json.loads(text)
        except json.JSONDecodeError as exc:
            raise DockerError(f"could not parse redis JSON for {hash_name!r}: {text[:200]!r}") from exc

        # cjson encodes an empty table as {}, a populated one as an array.
        if isinstance(data, dict):
            return {str(k): str(v) for k, v in data.items()}
        if isinstance(data, list):
            if len(data) % 2 != 0:
                raise DockerError(f"unexpected HGETALL payload for {hash_name!r}: {data!r}")
            return {str(data[i]): str(data[i + 1]) for i in range(0, len(data), 2)}
        raise DockerError(f"unexpected HGETALL payload for {hash_name!r}: {data!r}")

    def _query(self, query: str) -> list[dict]:
        output = self.docker.exec(
            MYSQL_SERVICE,
            [
                "mysql",
                "--batch",
                "--raw",
                "--skip-column-names",
                f"--user={self.config.dbuser}",
                f"--database={self.config.dbname}",
                "-e",
                query,
            ],
            env={"MYSQL_PWD": self.config.dbpass},
        )
        output = output.strip()
        if not output or output == "NULL":
            return []
        try:
            rows = json.loads(output)
        except json.JSONDecodeError as exc:
            raise DockerError(f"could not parse MySQL JSON output: {output[:300]!r}") from exc
        return rows if isinstance(rows, list) else []

    # ------------------------------------------------------------------ DKIM

    def _dkim_keys(self) -> dict[str, DkimKey]:
        public = self.redis_hgetall(REDIS_DKIM_PUB)
        selectors = self.redis_hgetall(REDIS_DKIM_SELECTORS)
        private = self.redis_hgetall(REDIS_DKIM_PRIV)

        keys: dict[str, DkimKey] = {}
        for domain, public_key in public.items():
            selector = selectors.get(domain)
            private_key = private.get(f"{selector}.{domain}") if selector else None
            keys[domain] = DkimKey(
                domain=domain,
                selector=selector,
                length=infer_key_length(public_key),
                public_key=public_key,
                private_key=private_key,
                dkim_txt=build_dkim_txt(public_key, split_255=self.config.split_dkim_255),
            )
        return keys

    def dkim_keys(self) -> dict[str, DkimKey]:
        return self.cache.get_or_set("dkim", self._dkim_keys)

    def dkim_for(self, domain: str) -> DkimKey | None:
        return self.dkim_keys().get(domain)

    # ------------------------------------------------------------------ domains

    def domains(self) -> list[dict]:
        return self.cache.get_or_set(
            "domains", lambda: self._query(_json_array_query("domain", DOMAIN_COLUMNS))
        )

    def alias_domains(self) -> list[dict]:
        return self.cache.get_or_set(
            "alias_domains",
            lambda: self._query(_json_array_query("alias_domain", ALIAS_DOMAIN_COLUMNS)),
        )

    def mailboxes(self) -> list[dict]:
        return self.cache.get_or_set(
            "mailboxes", lambda: self._query(_json_array_query("mailbox", MAILBOX_COLUMNS))
        )

    def aliases(self) -> list[dict]:
        return self.cache.get_or_set(
            "aliases", lambda: self._query(_json_array_query("alias", ALIAS_COLUMNS))
        )

    def invalidate_cache(self) -> None:
        self.cache.invalidate()
