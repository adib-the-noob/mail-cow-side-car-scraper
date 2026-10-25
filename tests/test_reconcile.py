"""Reconciler tests against an in-memory fake of the mailcow REST API."""

from __future__ import annotations

import json

import httpx
import pytest
from fastapi.testclient import TestClient

from app.config import SidecarSettings
from app.mailcow_api import MailcowApiClient, MailcowApiError
from app.models import (
    AliasDomainInfo,
    AliasInfo,
    DkimKey,
    DomainAdminInfo,
    DomainInfo,
    ExportBundle,
    MailboxInfo,
    RelayhostInfo,
    TransportInfo,
)
from app.reconcile import apply_plan, build_plan, desired_from_bundle
from app.main import create_app

PUB_KEY = "MIIBIjANBgkqhkiG9w0BAQEFAAOCAQ8AMIIBCgKCAQEATESTPUBKEY"
PRIV_KEY = "-----BEGIN PRIVATE KEY-----\nMIIEvQIBADANBg==\n-----END PRIVATE KEY-----\n"


def _state() -> dict:
    return {
        "domains": [],
        "dkim": {},
        "mailboxes": [],
        "aliases": [],
        "alias_domains": [],
        "relayhosts": [],
        "transports": [],
        "domain_admins": [],
        "writes": [],
    }


def _make_api(state: dict) -> MailcowApiClient:
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/api/v1/get/status/version":
            return httpx.Response(200, json={"version": "2024-01"})
        if request.method == "GET":
            if path == "/api/v1/get/domain/all":
                return httpx.Response(200, json=state["domains"])
            if path.startswith("/api/v1/get/dkim/"):
                domain = path.rsplit("/", 1)[1]
                return httpx.Response(200, json=state["dkim"].get(domain, []))
            if path == "/api/v1/get/mailbox/all":
                return httpx.Response(200, json=state["mailboxes"])
            if path == "/api/v1/get/alias/all":
                return httpx.Response(200, json=state["aliases"])
            if path == "/api/v1/get/alias-domain/all":
                return httpx.Response(200, json=state["alias_domains"])
            if path == "/api/v1/get/relayhost/all":
                return httpx.Response(200, json=state["relayhosts"])
            if path == "/api/v1/get/transport/all":
                return httpx.Response(200, json=state["transports"])
            if path == "/api/v1/get/domain-admin/all":
                return httpx.Response(200, json=state["domain_admins"])
        if request.method == "POST":
            body = json.loads(request.content) if request.content else None
            state["writes"].append({"path": path, "body": body})
            return httpx.Response(200, json={"type": "success", "msg": ["ok"]})
        return httpx.Response(404, json={"type": "error", "msg": "route not found"})

    client = httpx.Client(transport=httpx.MockTransport(handler), base_url="https://target.test")
    return MailcowApiClient("https://target.test", "rw-key", client=client)


def _desired_bundle() -> ExportBundle:
    return ExportBundle(
        generated_at="2024-01-01T00:00:00Z",
        mailcow_hostname="mail.example.org",
        domains=[
            DomainInfo(
                domain="example.org",
                description="Primary",
                aliases=400,
                mailboxes=10,
                quota=10240,
                maxquota=10240,
                defquota=3072,
                active=True,
                gal=True,
                backupmx=False,
                relay_all_recipients=False,
                dkim=DkimKey(
                    domain="example.org",
                    selector="dkim",
                    length="2048",
                    public_key=PUB_KEY,
                    private_key=PRIV_KEY,
                    dkim_txt="v=DKIM1;k=rsa;t=s;s=email;p=" + PUB_KEY,
                ),
            )
        ],
        alias_domains=[
            AliasDomainInfo(alias_domain="alias.example.net", target_domain="example.org", active=True)
        ],
        mailboxes=[
            MailboxInfo(
                username="postmaster@example.org",
                name="Postmaster",
                domain="example.org",
                local_part="postmaster",
                quota=3072,
                active=True,
            )
        ],
        aliases=[
            AliasInfo(address="sales@example.org", goto="postmaster@example.org", domain="example.org", active=True)
        ],
        relayhosts=[
            RelayhostInfo(
                hostname="relay.example.org:587",
                username="relayuser",
                password="relaypass",
                active=True,
            )
        ],
        transports=[
            TransportInfo(
                destination="remote.tld",
                nexthop="mx.remote.tld:25",
                username="tuser",
                password="tpass",
                active=True,
            )
        ],
        domain_admins=[
            DomainAdminInfo(username="deptadmin", domains=["example.org"], active=True)
        ],
    )


def _matching_actual() -> dict:
    state = _state()
    state["domains"] = [
        {
            "domain_name": "example.org",
            "description": "Primary",
            "active": "1",
            "max_num_aliases_for_domain": 400,
            "max_num_mboxes_for_domain": 10,
            "max_quota_for_domain": 10240 * 1024 * 1024,
            "max_quota_for_mbox": 10240 * 1024 * 1024,
            "def_quota_for_mbox": 3072 * 1024 * 1024,
            "backupmx": "0",
            "gal": "1",
            "relay_all_recipients": "0",
        }
    ]
    state["dkim"] = {"example.org": {"pubkey": PUB_KEY, "dkim_selector": "dkim"}}
    state["alias_domains"] = [
        {"alias_domain": "alias.example.net", "target_domain": "example.org", "active": "1"}
    ]
    state["aliases"] = [
        {"id": 7, "address": "sales@example.org", "goto": "postmaster@example.org", "domain": "example.org", "active": "1"}
    ]
    state["relayhosts"] = [
        {
            "id": 3,
            "hostname": "relay.example.org:587",
            "username": "relayuser",
            "password": "relaypass",
            "password_short": "rel...",
            "active": "1",
        }
    ]
    state["transports"] = [
        {
            "id": 4,
            "destination": "remote.tld",
            "nexthop": "mx.remote.tld:25",
            "username": "tuser",
            "password": "tpass",
            "password_short": "tpa...",
            "active": "1",
        }
    ]
    state["domain_admins"] = [
        {"username": "deptadmin", "active": "1", "selected_domains": ["example.org"], "unselected_domains": []}
    ]
    return state


# ---------------------------------------------------------------------- plan


def test_plan_creates_everything_on_empty_target() -> None:
    api = _make_api(_state())
    desired = desired_from_bundle(_desired_bundle())

    plan = build_plan(api, desired, target_url="https://target.test", source="bundle")

    actions = {(a.kind, a.action) for a in plan.actions}
    assert ("domain", "create") in actions
    assert ("dkim", "import-dkim") in actions
    assert ("alias-domain", "create") in actions
    assert ("alias", "create") in actions
    assert ("relayhost", "create") in actions
    assert ("transport", "create") in actions
    # Domain admins are opt-in (their password cannot be replicated).
    assert ("domain-admin", "create") not in actions
    # DKIM private key never leaks into the plan.
    dkim_action = next(a for a in plan.actions if a.kind == "dkim")
    assert dkim_action.payload is None
    assert plan.counts["create"] == 5  # domain, alias-domain, alias, relayhost, transport
    assert plan.counts["import-dkim"] == 1


def test_relayhost_credentials_round_trip_and_replace() -> None:
    api = _make_api(_state())
    desired = desired_from_bundle(_desired_bundle())

    plan = build_plan(api, desired, target_url="t", source="bundle")
    relay = next(a for a in plan.actions if a.kind == "relayhost")
    assert relay.payload["add"]["password"] == "relaypass"

    apply_plan(api, plan, desired)
    # No edit endpoint exists, so a changed relayhost is a replace, not an add.
    state = _matching_actual()
    state["relayhosts"][0]["password"] = "stale"
    api2 = _make_api(state)
    plan2 = build_plan(api2, desired, target_url="t", source="bundle")
    update = next(a for a in plan2.actions if a.kind == "relayhost")
    assert update.action == "update"
    assert update.payload["id"] == 3

    apply_plan(api2, plan2, desired)
    paths = [w["path"] for w in state["writes"]]
    assert "/api/v1/delete/relayhost" in paths
    assert "/api/v1/add/relayhost" in paths


def test_transport_change_is_replaced() -> None:
    state = _matching_actual()
    state["transports"][0]["nexthop"] = "old.remote.tld:25"
    api = _make_api(state)
    desired = desired_from_bundle(_desired_bundle())

    plan = build_plan(api, desired, target_url="t", source="bundle")
    transport = next(a for a in plan.actions if a.kind == "transport")
    assert transport.action == "update"
    assert transport.payload["add"]["password"] == "tpass"

    apply_plan(api, plan, desired)
    paths = [w["path"] for w in state["writes"]]
    assert paths == ["/api/v1/delete/transport", "/api/v1/add/transport"]


def test_domain_admins_opt_in_and_get_generated_password() -> None:
    state = _state()
    api = _make_api(state)
    desired = desired_from_bundle(_desired_bundle())

    off = build_plan(api, desired, target_url="t", source="bundle")
    assert [a for a in off.actions if a.kind == "domain-admin"] == []

    plan = build_plan(
        api, desired, target_url="t", source="bundle", include_domain_admins=True
    )
    admin = next(a for a in plan.actions if a.kind == "domain-admin")
    assert admin.target == "deptadmin"
    assert admin.payload["domains"] == "example.org"

    apply_plan(api, plan, desired)
    write = next(w for w in state["writes"] if w["path"] == "/api/v1/add/domain-admin")
    body = write["body"]
    assert body["password"] == body["password2"]
    assert body["password"]  # generated, since the original hash is unreadable


def test_domain_admin_grant_change_updates_in_place() -> None:
    state = _matching_actual()
    state["domain_admins"][0]["selected_domains"] = []
    api = _make_api(state)
    desired = desired_from_bundle(_desired_bundle())

    plan = build_plan(
        api, desired, target_url="t", source="bundle", include_domain_admins=True
    )
    admin = next(a for a in plan.actions if a.kind == "domain-admin")
    assert admin.action == "update"
    assert admin.payload["domains"] == ["example.org"]


def test_plan_is_empty_when_target_matches() -> None:
    api = _make_api(_matching_actual())
    desired = desired_from_bundle(_desired_bundle())

    plan = build_plan(api, desired, target_url="https://target.test", source="bundle")

    # Byte/MiB quota conversion must not cause spurious updates.
    assert plan.actions == []


def test_plan_detects_domain_and_alias_changes() -> None:
    state = _matching_actual()
    state["domains"][0]["description"] = "Old description"
    state["aliases"][0]["goto"] = "old@example.org"
    api = _make_api(state)
    desired = desired_from_bundle(_desired_bundle())

    plan = build_plan(api, desired, target_url="https://target.test", source="bundle")
    by_target = {a.target: a for a in plan.actions}

    assert by_target["example.org"].action == "update"
    assert by_target["sales@example.org"].action == "update"
    assert by_target["sales@example.org"].payload["id"] == 7


def test_plan_prune_is_conservative() -> None:
    state = _matching_actual()
    state["domains"].append({"domain_name": "stale.tld", "active": "1"})
    state["aliases"].append(
        {"id": 9, "address": "x@example.org", "goto": "postmaster@example.org", "domain": "example.org", "active": "1"}
    )
    state["aliases"].append(
        {"id": 10, "address": "y@other.tld", "goto": "z@other.tld", "domain": "other.tld", "active": "1"}
    )
    api = _make_api(state)
    desired = desired_from_bundle(_desired_bundle())

    plan = build_plan(api, desired, target_url="https://target.test", source="bundle", prune=True)
    deletes = {(a.kind, a.target) for a in plan.actions if a.action == "delete"}

    assert ("domain", "stale.tld") in deletes
    assert ("alias", "x@example.org") in deletes
    # Alias on an unmanaged domain is left alone.
    assert ("alias", "y@other.tld") not in deletes


def test_plan_no_delete_without_prune() -> None:
    state = _matching_actual()
    state["domains"].append({"domain_name": "stale.tld", "active": "1"})
    api = _make_api(state)
    desired = desired_from_bundle(_desired_bundle())

    plan = build_plan(api, desired, target_url="https://target.test", source="bundle")
    assert all(a.action != "delete" for a in plan.actions)


def test_mailboxes_opt_in() -> None:
    state = _matching_actual()
    api = _make_api(state)
    desired = desired_from_bundle(_desired_bundle())

    assert build_plan(api, desired, target_url="t", source="bundle").actions == []

    plan = build_plan(
        api, desired, target_url="t", source="bundle", include_mailboxes=True
    )
    mailbox_actions = [a for a in plan.actions if a.kind == "mailbox"]
    assert len(mailbox_actions) == 1
    assert mailbox_actions[0].payload["local_part"] == "postmaster"
    assert mailbox_actions[0].payload["quota"] == 3072


# ---------------------------------------------------------------------- apply


def test_apply_dispatches_writes() -> None:
    state = _state()
    api = _make_api(state)
    desired = desired_from_bundle(_desired_bundle())
    plan = build_plan(api, desired, target_url="https://target.test", source="bundle")

    result = apply_plan(api, plan, desired)

    assert result.failed == 0
    assert result.applied == len(plan.actions)

    paths = [w["path"] for w in state["writes"]]
    assert "/api/v1/add/domain" in paths
    assert "/api/v1/add/dkim_import" in paths
    assert "/api/v1/add/alias-domain" in paths
    assert "/api/v1/add/alias" in paths

    dkim_write = next(w for w in state["writes"] if w["path"] == "/api/v1/add/dkim_import")
    assert dkim_write["body"]["private_key_file"] == PRIV_KEY
    assert dkim_write["body"]["overwrite_existing"] == 1


def test_apply_mailbox_create_generates_password() -> None:
    state = _state()
    api = _make_api(state)
    desired = desired_from_bundle(_desired_bundle())
    plan = build_plan(api, desired, target_url="t", source="bundle", include_mailboxes=True)

    apply_plan(api, plan, desired)

    mailbox_write = next(w for w in state["writes"] if w["path"] == "/api/v1/add/mailbox")
    body = mailbox_write["body"]
    assert body["password"] == body["password2"]
    assert body["password"]
    assert body["force_pw_update"] == 1


def test_apply_records_api_errors() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(200, json=[])
        return httpx.Response(200, json={"type": "danger", "msg": "boom"})

    api = MailcowApiClient(
        "https://target.test",
        "rw-key",
        client=httpx.Client(transport=httpx.MockTransport(handler), base_url="https://target.test"),
    )
    desired = desired_from_bundle(_desired_bundle())
    # Handler returns [] for GETs, so domain/dkim are all "missing" -> creates.
    plan = build_plan(api, desired, target_url="t", source="bundle")

    result = apply_plan(api, plan, desired)
    assert result.failed == len(plan.actions)
    assert result.applied == 0
    assert "boom" in result.errors[0]


def test_api_client_reports_forbidden() -> None:
    handler = lambda request: httpx.Response(403)  # noqa: E731
    api = MailcowApiClient(
        "https://target.test",
        "ro-key",
        client=httpx.Client(transport=httpx.MockTransport(handler), base_url="https://target.test"),
    )
    with pytest.raises(MailcowApiError) as exc:
        api.list_domains()
    assert exc.value.status_code == 403


# ---------------------------------------------------------------------- endpoints


def test_receive_without_target_or_key_reports_503(
    mailcow_dir, stub_docker: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With auto-provisioning off and no key, receive mode cannot write."""
    monkeypatch.setenv("STUB_API_KEY", "none")  # mailcow has no rw key
    settings = SidecarSettings(
        mailcow_dir=str(mailcow_dir),
        docker_bin=stub_docker,
        cache_ttl=0,
        auto_api_key=False,
    )
    app = create_app(settings)
    with TestClient(app) as test_client:
        response = test_client.post(
            "/api/v1/replicate", json={"bundle": _desired_bundle().model_dump(mode="json")}
        )
    assert response.status_code == 503
    assert "no write target" in response.json()["detail"]


def test_replicate_requires_peer_or_bundle(client: TestClient) -> None:
    response = client.post("/api/v1/replicate", json={})
    assert response.status_code == 400
    assert "provide 'peer'" in response.json()["detail"]
