#!/usr/bin/env python3
"""A fake `docker` binary for tests.

Emulates just enough of `docker ps` / `docker exec` to exercise the sidecar
without a real mailcow install.
"""

from __future__ import annotations

import json
import os
import sys

PUB_A = "MIIBIjANBgkqhkiG9w0BAQEFAAOCAQ8AMIIBCgKCAQEA" + "A" * 402  # len 450 -> "2048"
PUB_B = "MIIBIjANBgkqhkiG9w0BAQEFAAOCAQ8AMIIBCgKCAQEA" + "B" * 402

# An existing rw API key in mailcow's `api` table.
DEFAULT_API_KEY = "AAAAAA-BBBBBB-CCCCCC-DDDDDD-EEEEEE-FFFFFF"

PRIV_A = (
    "-----BEGIN PRIVATE KEY-----\n"
    "MIIEvQIBADANBgkqhkiG9w0BAQEFAASCBKcwggSjAgEAAoIBAQDAAAAA\n"
    "-----END PRIVATE KEY-----\n"
)
PRIV_B = (
    "-----BEGIN PRIVATE KEY-----\n"
    "MIIEvQIBADANBgkqhkiG9w0BAQEFAASCBKcwggSjAgEAAoIBAQDBBBBB\n"
    "-----END PRIVATE KEY-----\n"
)

REDIS_HASHES = {
    "DKIM_PUB_KEYS": ["example.org", PUB_A, "alias.example.net", PUB_B],
    "DKIM_SELECTORS": ["example.org", "dkim", "alias.example.net", "mail"],
    "DKIM_PRIV_KEYS": ["dkim.example.org", PRIV_A, "mail.alias.example.net", PRIV_B],
}

DOMAIN_ROWS = [
    {
        "domain": "example.org",
        "description": "Primary domain",
        "aliases": 400,
        "mailboxes": 10,
        "defquota": 3072,
        "maxquota": 10240,
        "quota": 10240,
        "relayhost": "0",
        "backupmx": 0,
        "gal": 1,
        "relay_all_recipients": 0,
        "relay_unknown_only": 0,
        "active": 1,
        "created": "2024-01-01 00:00:00",
        "modified": None,
    },
    {
        "domain": "alias.example.net",
        "description": None,
        "aliases": 0,
        "mailboxes": 0,
        "defquota": 3072,
        "maxquota": 10240,
        "quota": 10240,
        "relayhost": "0",
        "backupmx": 0,
        "gal": 1,
        "relay_all_recipients": 0,
        "relay_unknown_only": 0,
        "active": 1,
        "created": "2024-02-01 00:00:00",
        "modified": None,
    },
]

MAILBOX_ROWS = [
    {
        "username": "postmaster@example.org",
        "name": "Postmaster",
        "domain": "example.org",
        "local_part": "postmaster",
        "quota": 3072,
        "active": 1,
        "authsource": "mailcow",
        "created": "2024-01-01 00:00:00",
        "modified": None,
    }
]

ALIAS_ROWS = [
    {
        "id": 1,
        "address": "sales@example.org",
        "goto": "postmaster@example.org",
        "domain": "example.org",
        "active": 1,
    }
]

# SMTP relay / transport credentials (mailcow keeps these in the clear).
RELAYHOST_ROWS = [
    {
        "id": 1,
        "hostname": "relay.example.org:587",
        "username": "relayuser",
        "password": "relaypass",
        "active": 1,
    }
]

TRANSPORT_ROWS = [
    {
        "id": 1,
        "destination": "remote.tld",
        "nexthop": "mx.remote.tld:25",
        "username": "tuser",
        "password": "tpass",
        "is_mx_based": 0,
        "active": 1,
    }
]

# Flat (username, domain) rows from the admin/domain_admins join.
DOMAIN_ADMIN_ROWS = [
    {"username": "deptadmin", "active": 1, "domain": "example.org"},
    {"username": "deptadmin", "active": 1, "domain": None},
]


def main() -> int:
    args = sys.argv[1:]
    if not args:
        return 0

    if args[0] == "ps":
        print("stub-container-id")
        return 0

    if args[0] == "exec":
        if "redis-cli" in args:
            hash_name = args[-1]
            data = REDIS_HASHES.get(hash_name)
            if data is None:
                print("{}")
            else:
                print(json.dumps(data))
            return 0

        if "mysql" in args:
            query = args[-1]
            # A pre-existing rw API key, so the sidecar reuses it instead of
            # creating one. Set STUB_API_KEY=none to simulate a fresh mailcow.
            if "FROM `api`" in query:
                key = os.environ.get("STUB_API_KEY", DEFAULT_API_KEY)
                print(json.dumps([] if key == "none" else [{"api_key": key}]))
                return 0
            if "FROM `domain`" in query:
                print(json.dumps(DOMAIN_ROWS))
            elif "FROM `alias_domain`" in query:
                print(
                    json.dumps(
                        [
                            {
                                "alias_domain": "alias.example.net",
                                "target_domain": "example.org",
                                "active": 1,
                            }
                        ]
                    )
                )
            elif "FROM `mailbox`" in query:
                print(json.dumps(MAILBOX_ROWS))
            elif "FROM `alias`" in query:
                print(json.dumps(ALIAS_ROWS))
            elif "FROM `relayhosts`" in query:
                print(json.dumps(RELAYHOST_ROWS))
            elif "FROM `transports`" in query:
                print(json.dumps(TRANSPORT_ROWS))
            elif "FROM `admin`" in query:
                print(json.dumps(DOMAIN_ADMIN_ROWS))
            else:
                print("[]")
            return 0

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
