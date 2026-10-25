"""Replication tests: the single ``/api/v1/replicate`` route and its client."""

from __future__ import annotations

import json

import httpx
import pytest
from fastapi.testclient import TestClient

import app.replicate as replicate_module
from app.config import SidecarSettings
from app.mailcow_api import MailcowApiClient
from app.main import create_app
from app.models import DkimKey, DomainInfo, ExportBundle
from app.replicate import PeerClient, PeerError, normalize_peer_url, send_bundle


class FakePeer:
    """A fake *receiving* sidecar: ``POST /api/v1/replicate``."""

    def __init__(self, status_code: int = 200, detail: str = "nope") -> None:
        self.status_code = status_code
        self.detail = detail
        self.received: list[dict] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v1/replicate":
            body = json.loads(request.content)
            self.received.append(body)
            if self.status_code >= 400:
                return httpx.Response(self.status_code, json={"detail": self.detail})
            return httpx.Response(
                200,
                json={
                    "mode": "receive",
                    "domains": len(body["bundle"]["domains"]),
                    "mailboxes": 0,
                    "aliases": 0,
                    "applied": 3,
                    "failed": 0,
                    "errors": [],
                },
            )
        return httpx.Response(404, json={"detail": "route not found"})

    def factory(self, base_url, *, verify_tls=True, timeout=120.0):
        """Stand-in for PeerClient that talks to this fake."""
        return PeerClient(
            base_url,
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


# ---------------------------------------------------------------------- url normalisation


@pytest.mark.parametrize(
    ("peer", "expected"),
    [
        ("10.0.0.2", "http://10.0.0.2:8800"),
        ("10.0.0.2:9000", "http://10.0.0.2:9000"),
        ("10.0.0.2/", "http://10.0.0.2:8800"),
        ("https://mail.example.org", "https://mail.example.org"),
        ("https://mail.example.org/", "https://mail.example.org"),
        ("http://10.0.0.2:8800", "http://10.0.0.2:8800"),
    ],
)
def test_normalize_peer_url(peer: str, expected: str) -> None:
    assert normalize_peer_url(peer) == expected


def test_normalize_peer_url_rejects_empty() -> None:
    with pytest.raises(PeerError):
        normalize_peer_url("   ")


# ---------------------------------------------------------------------- client


def test_send_bundle_posts_bundle_to_peer() -> None:
    peer = FakePeer()
    original = replicate_module.PeerClient
    replicate_module.PeerClient = peer.factory  # type: ignore[assignment]
    try:
        result = send_bundle(_bundle(), peer="10.0.0.2")
    finally:
        replicate_module.PeerClient = original  # type: ignore[assignment]

    assert result.mode == "send"
    assert result.peer == "http://10.0.0.2:8800"
    assert result.domains == 1
    assert result.applied == 3

    sent = peer.received[0]
    assert sent["bundle"]["mailcow_hostname"] == "mail.example.org"
    # Private keys travel with the bundle so the peer signs identically.
    assert sent["bundle"]["domains"][0]["dkim"]["private_key"].startswith("-----BEGIN")


def test_send_bundle_surfaces_peer_error() -> None:
    peer = FakePeer(status_code=503, detail="no write target configured")
    original = replicate_module.PeerClient
    replicate_module.PeerClient = peer.factory  # type: ignore[assignment]
    try:
        with pytest.raises(PeerError) as exc:
            send_bundle(_bundle(), peer="https://node2")
    finally:
        replicate_module.PeerClient = original  # type: ignore[assignment]

    assert exc.value.status_code == 503
    assert "no write target configured" in str(exc.value)


def test_send_bundle_unreachable_peer() -> None:
    def boom(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("nope", request=request)

    def factory(base_url, *, verify_tls=True, timeout=120.0):
        return PeerClient(
            base_url,
            client=httpx.Client(transport=httpx.MockTransport(boom), base_url=base_url),
        )

    original = replicate_module.PeerClient
    replicate_module.PeerClient = factory  # type: ignore[assignment]
    try:
        with pytest.raises(PeerError) as exc:
            send_bundle(_bundle(), peer="https://node2")
    finally:
        replicate_module.PeerClient = original  # type: ignore[assignment]

    assert "cannot reach peer" in str(exc.value)


# ---------------------------------------------------------------------- endpoint


def test_replicate_send_mode(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    peer = FakePeer()
    monkeypatch.setattr(replicate_module, "PeerClient", peer.factory)

    # Scrapes the local (stub) node and pushes to the fake peer.
    response = client.post(
        "/api/v1/replicate", json={"peer": "10.0.0.2", "include_mailboxes": True}
    )
    assert response.status_code == 200
    body = response.json()
    assert body["mode"] == "send"
    assert body["peer"] == "http://10.0.0.2:8800"
    assert body["domains"] == 2  # example.org + alias.example.net
    assert body["aliases"] == 1
    assert body["applied"] == 3

    sent = peer.received[0]
    assert sent["include_mailboxes"] is True
    assert {d["domain"] for d in sent["bundle"]["domains"]} == {"example.org", "alias.example.net"}


def test_replicate_send_mode_reports_unreachable_peer(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("nope", request=request)

    def factory(base_url, *, verify_tls=True, timeout=120.0):
        return PeerClient(
            base_url,
            client=httpx.Client(transport=httpx.MockTransport(boom), base_url=base_url),
        )

    monkeypatch.setattr(replicate_module, "PeerClient", factory)
    response = client.post("/api/v1/replicate", json={"peer": "10.0.0.2"})
    assert response.status_code == 502
    assert "cannot reach peer" in response.json()["detail"]


def test_replicate_receive_mode(
    mailcow_dir, stub_docker: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Export from this node, then post the bundle back to apply it locally."""
    writes: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(200, json=[])  # target starts empty -> everything creates
        writes.append(request.url.path)
        return httpx.Response(200, json={"type": "success", "msg": ["ok"]})

    def fake_api(base_url, api_key, *, verify_tls=True, timeout=30.0):
        return MailcowApiClient(
            base_url,
            api_key,
            client=httpx.Client(transport=httpx.MockTransport(handler), base_url=base_url),
        )

    monkeypatch.setattr("app.main.MailcowApiClient", fake_api)

    settings = SidecarSettings(
        mailcow_dir=str(mailcow_dir),
        docker_bin=stub_docker,
        cache_ttl=0,
        target_url="https://node2.test",
        target_api_key="rw-key",
    )
    app = create_app(settings)
    with TestClient(app) as test_client:
        bundle = test_client.get("/api/v1/export").json()
        response = test_client.post("/api/v1/replicate", json={"bundle": bundle})

    assert response.status_code == 200
    body = response.json()
    assert body["mode"] == "receive"
    assert body["failed"] == 0
    assert body["applied"] > 0
    assert "/api/v1/add/domain" in writes
