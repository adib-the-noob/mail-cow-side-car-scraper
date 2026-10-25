"""End-to-end tests against the stub Docker binary."""

from __future__ import annotations

from fastapi.testclient import TestClient


def test_root_and_health(client: TestClient) -> None:
    assert client.get("/").status_code == 200

    health = client.get("/health")
    assert health.status_code == 200
    body = health.json()
    assert body["status"] == "ok"
    assert all(service["ok"] for service in body["services"])


def test_status(client: TestClient) -> None:
    body = client.get("/api/v1/status").json()
    assert body["mailcow_hostname"] == "mail.example.org"
    assert body["compose_project"] == "mailcowdockerized"
    assert body["show_dkim_priv_keys"] is False


def test_domains_include_dkim_and_dns(client: TestClient) -> None:
    domains = client.get("/api/v1/domains").json()
    names = {d["domain"] for d in domains}
    assert names == {"example.org", "alias.example.net"}

    example = next(d for d in domains if d["domain"] == "example.org")
    assert example["active"] is True
    assert example["is_alias_domain"] is False
    assert example["dkim"]["selector"] == "dkim"
    assert example["dkim"]["private_key"].startswith("-----BEGIN PRIVATE KEY-----")

    types = {(r["name"], r["type"]) for r in example["dns"]}
    assert ("example.org", "MX") in types
    assert ("dkim._domainkey.example.org", "TXT") in types

    alias = next(d for d in domains if d["domain"] == "alias.example.net")
    assert alias["is_alias_domain"] is True


def test_dkim_private_key_roundtrip(client: TestClient) -> None:
    key = client.get("/api/v1/dkim/example.org").json()
    assert key["length"] == "2048"
    # Multi-line PEM survived the Redis -> JSON round-trip.
    assert key["private_key"].count("\n") >= 2
    assert key["dkim_txt"].startswith("v=DKIM1;k=rsa;t=s;s=email;p=")


def test_dkim_unknown_domain_404(client: TestClient) -> None:
    assert client.get("/api/v1/dkim/nope.tld").status_code == 404


def test_dns_endpoints(client: TestClient) -> None:
    all_dns = client.get("/api/v1/dns").json()
    assert set(all_dns) == {"example.org", "alias.example.net"}

    records = client.get("/api/v1/dns/example.org").json()
    assert any(r["type"] == "MX" for r in records)
    assert client.get("/api/v1/dns/nope.tld").status_code == 404


def test_mailboxes_and_aliases(client: TestClient) -> None:
    mailboxes = client.get("/api/v1/mailboxes").json()
    assert mailboxes[0]["username"] == "postmaster@example.org"

    assert client.get("/api/v1/mailboxes?domain=example.org").json() == mailboxes
    assert client.get("/api/v1/mailboxes?domain=other.tld").json() == []

    aliases = client.get("/api/v1/aliases").json()
    assert aliases[0]["address"] == "sales@example.org"
    assert client.get("/api/v1/aliases?domain=other.tld").json() == []

    alias_domains = client.get("/api/v1/alias-domains").json()
    assert alias_domains[0]["target_domain"] == "example.org"


def test_export_bundle(client: TestClient) -> None:
    bundle = client.get("/api/v1/export").json()
    assert bundle["mailcow_hostname"] == "mail.example.org"
    assert len(bundle["domains"]) == 2
    assert bundle["generated_at"]


def test_bearer_auth_enforced(authed_client: TestClient) -> None:
    assert authed_client.get("/api/v1/domains").status_code == 401

    ok = authed_client.get(
        "/api/v1/domains", headers={"Authorization": "Bearer s3cret"}
    )
    assert ok.status_code == 200

    # Health/root stay open.
    assert authed_client.get("/health").status_code == 200
