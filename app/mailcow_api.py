"""Client for mailcow's REST API (the write/injection path).

Used by the reconciler to push configuration into another mailcow node. This is
the *only* place in the project that talks to the mailcow HTTP API rather than
reading the containers directly.

Requirements on the target:
* an API key with ``access='rw'`` (an ``ro`` key gets 403 on writes),
* the caller's IP in the key's ``allow_from`` (or ``skip_ip_check=1``),
* a non-browser client (requests with a ``Sec-Fetch-Dest`` header are rejected).

``dkim_import`` is implemented by mailcow (``json_api.php`` ->
``dkim('import', ...)``) but is absent from the published OpenAPI spec, which is
why it lives here explicitly.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import quote

import httpx

# The GET payload uses different keys than the POST bodies; see reconcile.py.
_SENTINEL = object()


class MailcowApiError(RuntimeError):
    def __init__(self, message: str, status_code: int | None = None, payload: Any = None) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.payload = payload


class MailcowApiClient:
    def __init__(
        self,
        base_url: str,
        api_key: str,
        *,
        verify_tls: bool = True,
        timeout: float = 30.0,
        client: httpx.Client | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self._owns_client = client is None
        self._client = client or httpx.Client(
            base_url=self.base_url,
            headers={"X-API-Key": api_key, "Accept": "application/json"},
            verify=verify_tls,
            timeout=timeout,
            follow_redirects=True,
        )

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def __enter__(self) -> "MailcowApiClient":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # ------------------------------------------------------------------ transport

    def _request(self, method: str, path: str, json_body: Any = None) -> Any:
        try:
            response = self._client.request(method, path, json=json_body)
        except httpx.HTTPError as exc:
            raise MailcowApiError(f"{method} {path} failed: {exc}") from exc

        if response.status_code == 401:
            raise MailcowApiError("unauthorized: check X-API-Key", 401)
        if response.status_code == 403:
            raise MailcowApiError(
                "forbidden: read-only key, caller IP not in allow_from, or a browser "
                "(Sec-Fetch-Dest) request",
                403,
            )

        try:
            payload = response.json()
        except ValueError as exc:
            raise MailcowApiError(
                f"non-JSON response (HTTP {response.status_code}): {response.text[:300]!r}",
                response.status_code,
            ) from exc

        if response.status_code >= 400:
            raise MailcowApiError(f"HTTP {response.status_code}: {payload}", response.status_code, payload)
        return payload

    def _write(self, path: str, body: Any) -> Any:
        payload = self._request("POST", path, body)
        # mailcow answers writes with 200 + {"type": "error"|"danger"|"success"}.
        items = payload if isinstance(payload, list) else [payload]
        for item in items:
            if isinstance(item, dict) and item.get("type") in {"error", "danger"}:
                raise MailcowApiError(
                    f"mailcow rejected {path}: {item.get('msg')}", payload=item
                )
        return payload

    @staticmethod
    def _seg(value: str) -> str:
        return quote(value, safe="")

    # ------------------------------------------------------------------ reads

    def ping(self) -> dict:
        return self._request("GET", "/api/v1/get/status/version")

    def list_domains(self) -> list[dict]:
        data = self._request("GET", "/api/v1/get/domain/all")
        return data if isinstance(data, list) else []

    def get_dkim(self, domain: str) -> dict | None:
        data = self._request("GET", f"/api/v1/get/dkim/{self._seg(domain)}")
        return data if isinstance(data, dict) and data else None

    def list_mailboxes(self) -> list[dict]:
        data = self._request("GET", "/api/v1/get/mailbox/all")
        return data if isinstance(data, list) else []

    def list_aliases(self) -> list[dict]:
        data = self._request("GET", "/api/v1/get/alias/all")
        return data if isinstance(data, list) else []

    def list_alias_domains(self) -> list[dict]:
        data = self._request("GET", "/api/v1/get/alias-domain/all")
        return data if isinstance(data, list) else []

    # ------------------------------------------------------------------ domain writes

    def add_domain(self, attrs: dict) -> Any:
        return self._write("/api/v1/add/domain", attrs)

    def edit_domain(self, domain: str, attrs: dict) -> Any:
        return self._write("/api/v1/edit/domain", {"items": [domain], "attr": attrs})

    def delete_domain(self, domain: str) -> Any:
        return self._write("/api/v1/delete/domain", [domain])

    # ------------------------------------------------------------------ dkim writes

    def import_dkim(self, domain: str, selector: str, private_key: str, *, overwrite: bool = True) -> Any:
        return self._write(
            "/api/v1/add/dkim_import",
            {
                "domain": domain,
                "dkim_selector": selector,
                "private_key_file": private_key,
                "overwrite_existing": 1 if overwrite else 0,
            },
        )

    def delete_dkim(self, domain: str) -> Any:
        return self._write("/api/v1/delete/dkim", [domain])

    # ------------------------------------------------------------------ alias writes

    def add_alias(self, attrs: dict) -> Any:
        return self._write("/api/v1/add/alias", attrs)

    def edit_alias(self, alias_id: int, attrs: dict) -> Any:
        return self._write("/api/v1/edit/alias", {"items": [alias_id], "attr": attrs})

    def delete_alias(self, alias_id: int) -> Any:
        return self._write("/api/v1/delete/alias", [alias_id])

    # ------------------------------------------------------------------ alias domain writes

    def add_alias_domain(self, attrs: dict) -> Any:
        return self._write("/api/v1/add/alias-domain", attrs)

    def edit_alias_domain(self, alias_domain: str, attrs: dict) -> Any:
        return self._write(
            "/api/v1/edit/alias-domain", {"items": [alias_domain], "attr": attrs}
        )

    def delete_alias_domain(self, alias_domain: str) -> Any:
        return self._write("/api/v1/delete/alias-domain", [alias_domain])

    # ------------------------------------------------------------------ mailbox writes

    def add_mailbox(self, attrs: dict) -> Any:
        return self._write("/api/v1/add/mailbox", attrs)

    def edit_mailbox(self, username: str, attrs: dict) -> Any:
        return self._write("/api/v1/edit/mailbox", {"items": [username], "attr": attrs})

    def delete_mailbox(self, username: str) -> Any:
        return self._write("/api/v1/delete/mailbox", [username])
