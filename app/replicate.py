"""Peer-to-peer replication between two sidecars.

Node 1 runs a *full* mailcow; Node 2 runs a *fresh* mailcow. Both run this
sidecar. You POST Node 2's address to Node 1 and Node 1 ships its configuration
to Node 2, which applies it to its own mailcow.

The whole feature is one route on both ends:

* **send** — ``POST /api/v1/replicate {"peer": "10.0.0.2"}`` scrapes this node and
  POSTs the bundle to ``{peer}/api/v1/replicate``.
* **receive** — ``POST /api/v1/replicate {"bundle": {...}}`` applies that bundle
  to this node's own mailcow (via ``SIDECAR_TARGET_URL``).
"""

from __future__ import annotations

from typing import Any

import httpx

from .models import ExportBundle, ReplicateResponse

DEFAULT_PEER_PORT = 8800


class PeerError(RuntimeError):
    def __init__(self, message: str, status_code: int | None = None, payload: Any = None) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.payload = payload


def _has_port(host: str) -> bool:
    if host.startswith("["):  # IPv6 literal, e.g. [::1]:8800
        return "]:" in host
    return ":" in host


def normalize_peer_url(peer: str) -> str:
    """Accept a bare IP/host and turn it into a base URL.

    ``10.0.0.2`` -> ``http://10.0.0.2:8800``
    ``10.0.0.2:9000`` -> ``http://10.0.0.2:9000``
    ``https://mail.example.org`` -> unchanged
    """
    peer = peer.strip().rstrip("/")
    if not peer:
        raise PeerError("empty peer address")

    if "://" not in peer:
        host = peer if _has_port(peer) else f"{peer}:{DEFAULT_PEER_PORT}"
        peer = f"http://{host}"
    return peer


class PeerClient:
    """Client for another sidecar's ``/api/v1/replicate``."""

    def __init__(
        self,
        base_url: str,
        *,
        verify_tls: bool = True,
        timeout: float = 120.0,
        client: httpx.Client | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self._owns_client = client is None
        self._client = client or httpx.Client(
            base_url=self.base_url,
            headers={"Accept": "application/json"},
            verify=verify_tls,
            timeout=timeout,
            follow_redirects=True,
        )

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def __enter__(self) -> "PeerClient":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def send(
        self,
        bundle: dict,
        *,
        prune: bool = False,
        include_mailboxes: bool = False,
        include_domain_admins: bool = False,
    ) -> dict:
        body = {
            "bundle": bundle,
            "prune": prune,
            "include_mailboxes": include_mailboxes,
            "include_domain_admins": include_domain_admins,
        }
        try:
            response = self._client.post("/api/v1/replicate", json=body)
        except httpx.HTTPError as exc:
            raise PeerError(f"cannot reach peer {self.base_url}: {exc}") from exc

        try:
            payload = response.json()
        except ValueError:
            payload = None

        if response.status_code >= 400:
            detail = payload.get("detail") if isinstance(payload, dict) else payload
            raise PeerError(
                f"peer returned HTTP {response.status_code}: {detail}",
                response.status_code,
                payload,
            )
        if payload is None:
            raise PeerError("peer returned a non-JSON response", response.status_code)
        return payload


def send_bundle(
    bundle: ExportBundle,
    *,
    peer: str,
    verify_tls: bool = True,
    timeout: float = 120.0,
    prune: bool = False,
    include_mailboxes: bool = False,
    include_domain_admins: bool = False,
) -> ReplicateResponse:
    """Ship ``bundle`` to a peer sidecar, which applies it to its own mailcow."""
    base_url = normalize_peer_url(peer)

    with PeerClient(base_url, verify_tls=verify_tls, timeout=timeout) as client:
        raw = client.send(
            bundle.model_dump(mode="json"),
            prune=prune,
            include_mailboxes=include_mailboxes,
            include_domain_admins=include_domain_admins,
        )

    try:
        peer_response = ReplicateResponse.model_validate(raw)
    except Exception as exc:  # noqa: BLE001 - peer contract mismatch
        raise PeerError(f"unexpected response from peer: {exc}", payload=raw) from exc

    return ReplicateResponse(
        mode="send",
        peer=base_url,
        domains=len(bundle.domains),
        mailboxes=len(bundle.mailboxes),
        aliases=len(bundle.aliases),
        applied=peer_response.applied,
        failed=peer_response.failed,
        errors=peer_response.errors,
    )
