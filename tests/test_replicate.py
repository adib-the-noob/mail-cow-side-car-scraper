"""Peer-to-peer replication tests against a fake peer sidecar."""

from __future__ import annotations

import json

import httpx
import pytest
from fastapi.testclient import TestClient

import app.replicate as replicate_module
from app.models import DkimKey, DomainInfo, ExportBundle
from app.replicate import PeerClient, PeerError, preview_bundle, push_bundle


class FakePeer:
    """A fake sidecar: /health, /api/v1/status, reconcile/plan and reconcile/apply."""

    def __init__(self, hostname: str = "node2.example.org", token: str | None = None) -> None:
        self.hostname = hostname
        self.token = token
        self.applied: list[dict] = []
        self.planned: list[dict] = []

    @property
    def requests(self) -> list[dict]:
        return self.applied

    def _plan_json(self, body: dict) -> dict:
        return {
            "target_url": "https://node2",
            "source": "bundle",
            "generated_at": "2024-01-01T00:00:00Z",
            "prune": body.get("prune", False),
            "include_mailboxes": body.get("include_mailboxes", False),
            "actions": [
                {
                    "kind": "domain",
                    "action": "create",
                    "target": "example.org",
                    "detail": "domain missing on target",
                    "payload": {"domain": "example.org"},
                }
            ],
            "counts": {"create": 1},
        }

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        # /health is public, exactly like the real app; /api/v1/* needs the token.
        if path.startswith("/api/v1/") and self.token is not None:
            if request.headers.get("Authorization") != f"Bearer {self.token}":
                return httpx.Response(401, json={"detail": "invalid or missing bearer token"})

        if path == "/health":
            return httpx.Response(200, json={"status": "ok", "ready": True, "services": []})
        if path == "/api/v1/status":
            return httpx.Response(
                200,
                json={
                    "version": "1.0.0",
                    "mailcow_dir": "/opt/mailcow-dockerized",
                    "mailcow_hostname": self.hostname,
                    "compose_project": "mailcowdockerized",
                    "split_dkim_255": False,
                    "show_dkim_priv_keys": False,
                    "services": [],
                },
            )
        if path == "/api/v1/reconcile/plan":
            body = json.loads(request.content)
            self.planned.append(body)
            return httpx.Response(200, json=self._plan_json(body))

        if path == "/api/v1/reconcile/apply":
            body = json.loads(request.content)
            self.applied.append(body)
            return httpx.Response(
                200,
                json={
                    "plan": self._plan_json(body),
                    "applied": 1,
                    "failed": 0,
                    "errors": [],
                },
            )
        return httpx.Response(404, json={"detail": "route not found"})

    def factory(self, base_url, token=None, *, verify_tls=True, timeout=120.0):
        """Stand-in for PeerClient that talks to this fake."""
        return PeerClient(
            base_url,
            token,
            client=httpx.Client(transport=httpx.MockTransport(self.handler), base_url=base_url),
        )


def _bundle() -> ExportBundle:
    return ExportBundle(
        generated_at="2024-01-01T00:00:00Z",
        mailcow_hostname="mail.example.org",
        domains=[
            DomainInfo(
                domain="example.org",
                active=True,
                dkim=DkimKey(
                    domain="example.org",
                    selector="dkim",
                    public_key="PUB",
                    private_key="-----BEGIN PRIVATE KEY-----\nX\n-----END PRIVATE KEY-----\n",
                    dkim_txt="v=DKIM1;k=rsa;t=s;s=email;p=PUB",
                ),
            )
        ],
        alias_domains=[],
    )


# ---------------------------------------------------------------------- peer client


def test_probe_peer_reports_hostname() -> None:
    peer = FakePeer(hostname="node2.example.org")
    info = PeerClient(
        "https://node2", "tok", client=httpx.Client(transport=httpx.MockTransport(peer.handler), base_url="https://node2")
    ).info()
    assert info.reachable is True
    assert info.authenticated is True
    assert info.mailcow_hostname == "node2.example.org"
    assert info.version == "1.0.0"


def test_probe_peer_without_token() -> None:
    peer = FakePeer()
    info = PeerClient(
        "https://node2", None, client=httpx.Client(transport=httpx.MockTransport(peer.handler), base_url="https://node2")
    ).info()
    assert info.reachable is True
    assert info.authenticated is None
    assert "no peer token" in (info.detail or "")


def test_probe_peer_with_bad_token() -> None:
    peer = FakePeer(token="right")
    info = PeerClient(
        "https://node2", "wrong", client=httpx.Client(transport=httpx.MockTransport(peer.handler), base_url="https://node2")
    ).info()
    assert info.reachable is True
    assert info.authenticated is False


def test_probe_peer_unreachable() -> None:
    def boom(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("nope", request=request)

    info = PeerClient(
        "https://node2", None, client=httpx.Client(transport=httpx.MockTransport(boom), base_url="https://node2")
    ).info()
    assert info.reachable is False
    assert "cannot reach peer" in (info.detail or "")


def test_push_bundle_sends_bundle_and_returns_result() -> None:
    peer = FakePeer()

    # push_bundle builds its own client; patch PeerClient to use the fake transport.
    original = replicate_module.PeerClient
    replicate_module.PeerClient = peer.factory  # type: ignore[assignment]
    try:
        result = push_bundle(_bundle(), peer_url="https://node2", prune=False)
    finally:
        replicate_module.PeerClient = original  # type: ignore[assignment]

    assert result.mailcow_hostname == "mail.example.org"
    assert result.domains == 1
    assert result.peer_result is not None
    assert result.peer_result.applied == 1

    sent = peer.applied[0]
    assert sent["bundle"]["mailcow_hostname"] == "mail.example.org"
    # Private keys travel with the bundle so the peer signs identically.
    assert sent["bundle"]["domains"][0]["dkim"]["private_key"].startswith("-----BEGIN")


def test_push_bundle_surfaces_auth_failure() -> None:
    peer = FakePeer(token="right")
    original = replicate_module.PeerClient
    replicate_module.PeerClient = peer.factory  # type: ignore[assignment]
    try:
        with pytest.raises(PeerError) as exc:
            push_bundle(_bundle(), peer_url="https://node2", token="wrong")
    finally:
        replicate_module.PeerClient = original  # type: ignore[assignment]
    assert exc.value.status_code == 401


# ---------------------------------------------------------------------- endpoints


def test_preview_bundle_returns_plan_without_writing() -> None:
    peer = FakePeer()
    original = replicate_module.PeerClient
    replicate_module.PeerClient = peer.factory  # type: ignore[assignment]
    try:
        plan = preview_bundle(_bundle(), peer_url="https://node2")
    finally:
        replicate_module.PeerClient = original  # type: ignore[assignment]

    assert plan.counts == {"create": 1}
    assert plan.actions[0].target == "example.org"
    assert peer.applied == []  # preview must not write
    assert len(peer.planned) == 1


def test_replicate_preview_endpoint(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    peer = FakePeer()
    monkeypatch.setattr(replicate_module, "PeerClient", peer.factory)

    response = client.post("/api/v1/replicate/preview", json={"peer_url": "https://node2"})
    assert response.status_code == 200
    assert response.json()["counts"] == {"create": 1}
    assert peer.applied == []


def test_replicate_push_endpoint(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    peer = FakePeer()
    monkeypatch.setattr(replicate_module, "PeerClient", peer.factory)

    # Scrapes the local (stub) node and pushes to the fake peer.
    response = client.post(
        "/api/v1/replicate/push",
        json={"peer_url": "https://node2", "include_mailboxes": True},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["domains"] == 2  # example.org + alias.example.net
    assert body["aliases"] == 1
    assert body["peer_result"]["applied"] == 1

    sent = peer.applied[0]
    assert sent["include_mailboxes"] is True
    assert {d["domain"] for d in sent["bundle"]["domains"]} == {"example.org", "alias.example.net"}


def test_replicate_peer_endpoint(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    peer = FakePeer(hostname="node2.example.org")
    monkeypatch.setattr(replicate_module, "PeerClient", peer.factory)

    response = client.post(
        "/api/v1/replicate/peer", json={"peer_url": "https://node2", "peer_token": "tok"}
    )
    assert response.status_code == 200
    body = response.json()
    assert body["reachable"] is True
    assert body["mailcow_hostname"] == "node2.example.org"


def test_replicate_push_reports_unreachable_peer(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("nope", request=request)

    def factory(base_url, token=None, *, verify_tls=True, timeout=120.0):
        return PeerClient(
            base_url,
            token,
            client=httpx.Client(transport=httpx.MockTransport(boom), base_url=base_url),
        )

    monkeypatch.setattr(replicate_module, "PeerClient", factory)
    response = client.post("/api/v1/replicate/push", json={"peer_url": "https://node2"})
    assert response.status_code == 502
    assert "cannot reach peer" in response.json()["detail"]
