"""Peer-to-peer replication between two sidecars.

Topology: Node 1 runs a *full* mailcow; Node 2 runs a *fresh* mailcow. Both run
this sidecar. You paste Node 2's URL into Node 1, Node 1 scrapes itself and
POSTs the resulting bundle to Node 2's ``/api/v1/reconcile/apply``, and Node 2
provisions its own containers from it.

Node 2 therefore needs its own write path configured
(``SIDECAR_TARGET_URL`` pointing at its own mailcow + an ``rw`` API key); the
receiver-facing apply logic lives in :mod:`app.reconcile` and is reused as-is.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import httpx

from .models import ExportBundle, PeerInfo, ReconcilePlan, ReconcileResult, ReplicateResult


class PeerError(RuntimeError):
    def __init__(self, message: str, status_code: int | None = None, payload: Any = None) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.payload = payload


class PeerClient:
    """Client for another sidecar's HTTP API."""

    def __init__(
        self,
        base_url: str,
        token: str | None = None,
        *,
        verify_tls: bool = True,
        timeout: float = 120.0,
        client: httpx.Client | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.token = token
        headers = {"Accept": "application/json"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        self._owns_client = client is None
        self._client = client or httpx.Client(
            base_url=self.base_url,
            headers=headers,
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

    def _request(self, method: str, path: str, json_body: Any = None) -> Any:
        try:
            response = self._client.request(method, path, json=json_body)
        except httpx.HTTPError as exc:
            raise PeerError(f"cannot reach peer {self.base_url}: {exc}") from exc

        try:
            payload = response.json()
        except ValueError:
            payload = None

        if response.status_code == 401:
            raise PeerError("peer rejected the token (401)", 401, payload)
        if response.status_code >= 400:
            detail = payload.get("detail") if isinstance(payload, dict) else payload
            raise PeerError(
                f"peer returned HTTP {response.status_code}: {detail}", response.status_code, payload
            )
        if payload is None:
            raise PeerError("peer returned a non-JSON response", response.status_code)
        return payload

    # ------------------------------------------------------------------ probes

    def info(self) -> PeerInfo:
        info = PeerInfo(url=self.base_url, reachable=False)
        try:
            health = self._request("GET", "/health")
        except PeerError as exc:
            info.detail = str(exc)
            return info

        info.reachable = True
        if not isinstance(health, dict):
            return info

        if self.token:
            try:
                status = self._request("GET", "/api/v1/status")
                info.authenticated = True
                info.mailcow_hostname = status.get("mailcow_hostname")
                info.compose_project = status.get("compose_project")
                info.version = status.get("version")
            except PeerError as exc:
                info.authenticated = False
                info.detail = str(exc)
        else:
            info.detail = "no peer token supplied; only the unauthenticated /health probe ran"
        return info

    # ------------------------------------------------------------------ push

    def plan(
        self, bundle: dict, *, prune: bool = False, include_mailboxes: bool = False
    ) -> dict:
        return self._request(
            "POST",
            "/api/v1/reconcile/plan",
            {"bundle": bundle, "prune": prune, "include_mailboxes": include_mailboxes},
        )

    def apply(
        self, bundle: dict, *, prune: bool = False, include_mailboxes: bool = False
    ) -> dict:
        return self._request(
            "POST",
            "/api/v1/reconcile/apply",
            {"bundle": bundle, "prune": prune, "include_mailboxes": include_mailboxes},
        )


def probe_peer(
    peer_url: str,
    *,
    token: str | None = None,
    verify_tls: bool = True,
    timeout: float = 120.0,
) -> PeerInfo:
    with PeerClient(peer_url, token, verify_tls=verify_tls, timeout=timeout) as peer:
        return peer.info()


def preview_bundle(
    bundle: ExportBundle,
    *,
    peer_url: str,
    token: str | None = None,
    verify_tls: bool = True,
    timeout: float = 120.0,
    prune: bool = False,
    include_mailboxes: bool = False,
) -> ReconcilePlan:
    """Ask a peer what it *would* change, without writing anything."""
    with PeerClient(peer_url, token, verify_tls=verify_tls, timeout=timeout) as peer:
        raw = peer.plan(bundle.model_dump(mode="json"), prune=prune, include_mailboxes=include_mailboxes)

    try:
        return ReconcilePlan.model_validate(raw)
    except Exception as exc:  # noqa: BLE001 - peer contract mismatch
        raise PeerError(f"unexpected response from peer plan: {exc}", payload=raw) from exc


def push_bundle(
    bundle: ExportBundle,
    *,
    peer_url: str,
    token: str | None = None,
    verify_tls: bool = True,
    timeout: float = 120.0,
    prune: bool = False,
    include_mailboxes: bool = False,
) -> ReplicateResult:
    """Send ``bundle`` to a peer sidecar, which applies it to its own mailcow."""
    with PeerClient(peer_url, token, verify_tls=verify_tls, timeout=timeout) as peer:
        raw = peer.apply(bundle.model_dump(mode="json"), prune=prune, include_mailboxes=include_mailboxes)

    try:
        peer_result = ReconcileResult.model_validate(raw)
    except Exception as exc:  # noqa: BLE001 - peer contract mismatch
        raise PeerError(f"unexpected response from peer apply: {exc}", payload=raw) from exc

    return ReplicateResult(
        peer_url=peer_url.rstrip("/"),
        pushed_at=datetime.now(timezone.utc).isoformat(),
        mailcow_hostname=bundle.mailcow_hostname,
        domains=len(bundle.domains),
        mailboxes=len(bundle.mailboxes),
        aliases=len(bundle.aliases),
        peer_result=peer_result,
    )
