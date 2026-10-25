"""Reconciler: push configuration from a source into another mailcow node.

The mailcow API's GET responses and POST bodies use *different* key names and
units (GET returns quotas in bytes under ``max_quota_for_*``; POST expects MiB
under ``quota``/``maxquota``/``defquota``). The mappings below bridge that, and
are the reason this module is more than a thin wrapper.

Design notes:
* ``plan`` is a pure diff and never writes.
* ``apply`` executes a plan.
* DKIM private keys are never put into a plan payload; ``apply`` re-reads them
  from the desired state, so plans are safe to log/serve.
* Deletions only happen with ``prune=True``, and alias pruning is limited to
  domains we manage.
* Mailbox passwords are stored hashed in MariaDB and cannot be scraped, so
  created mailboxes get a random password + ``force_pw_update``.
"""

from __future__ import annotations

import argparse
import json
import secrets
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable

from .config import MailcowConfig, SidecarSettings
from .docker_exec import DockerClient
from .mailcow import MailcowClient, WriteTargetError
from .mailcow_api import MailcowApiClient, MailcowApiError
from .models import (
    AliasDomainInfo,
    AliasInfo,
    DkimKey,
    DomainAdminInfo,
    DomainInfo,
    ExportBundle,
    MailboxInfo,
    ReconcileAction,
    ReconcilePlan,
    ReconcileResult,
    RelayhostInfo,
    TransportInfo,
)
from .service import export_bundle

MIB = 1024 * 1024

# desired key -> (actual GET key, transform from desired value to the GET unit)
_DOMAIN_FIELDS: dict[str, tuple[str, Callable[[Any], Any]]] = {
    "description": ("description", lambda v: str(v or "")),
    "active": ("active", lambda v: int(bool(v))),
    "aliases": ("max_num_aliases_for_domain", lambda v: int(v or 0)),
    "mailboxes": ("max_num_mboxes_for_domain", lambda v: int(v or 0)),
    "quota": ("max_quota_for_domain", lambda v: int(v or 0) * MIB),
    "maxquota": ("max_quota_for_mbox", lambda v: int(v or 0) * MIB),
    "defquota": ("def_quota_for_mbox", lambda v: int(v or 0) * MIB),
    "backupmx": ("backupmx", lambda v: int(bool(v))),
    "gal": ("gal", lambda v: int(bool(v))),
    "relay_all_recipients": ("relay_all_recipients", lambda v: int(bool(v))),
}


@dataclass
class DesiredState:
    domains: dict[str, dict] = field(default_factory=dict)
    dkim: dict[str, DkimKey] = field(default_factory=dict)
    aliases: dict[str, dict] = field(default_factory=dict)
    alias_domains: dict[str, dict] = field(default_factory=dict)
    mailboxes: dict[str, dict] = field(default_factory=dict)
    relayhosts: dict[str, dict] = field(default_factory=dict)
    transports: dict[str, dict] = field(default_factory=dict)
    domain_admins: dict[str, dict] = field(default_factory=dict)


def desired_from_bundle(bundle: ExportBundle) -> DesiredState:
    alias_names = {alias.alias_domain for alias in bundle.alias_domains}

    domains: dict[str, dict] = {}
    dkim: dict[str, DkimKey] = {}
    for info in bundle.domains:
        if info.dkim is not None:
            dkim[info.domain] = info.dkim
        # Alias domains are managed via the alias-domain table, not `domain`.
        if info.is_alias_domain or info.domain in alias_names:
            continue
        domains[info.domain] = {
            "description": info.description,
            "active": info.active,
            "aliases": info.aliases,
            "mailboxes": info.mailboxes,
            "quota": info.quota,
            "maxquota": info.maxquota,
            "defquota": info.defquota,
            "backupmx": info.backupmx,
            "gal": info.gal,
            "relay_all_recipients": info.relay_all_recipients,
            "relay_unknown_only": info.relay_unknown_only,
        }

    return DesiredState(
        domains=domains,
        dkim={name: key for name, key in dkim.items() if name in domains or name in alias_names},
        aliases={
            alias.address: {
                "address": alias.address,
                "goto": alias.goto,
                "active": alias.active,
            }
            for alias in bundle.aliases
        },
        alias_domains={
            alias.alias_domain: {
                "alias_domain": alias.alias_domain,
                "target_domain": alias.target_domain,
                "active": alias.active,
            }
            for alias in bundle.alias_domains
        },
        mailboxes={
            mailbox.username: {
                "username": mailbox.username,
                "name": mailbox.name,
                "domain": mailbox.domain,
                "local_part": mailbox.local_part,
                "quota": mailbox.quota,
                "active": mailbox.active,
            }
            for mailbox in bundle.mailboxes
        },
        relayhosts={
            relayhost.hostname: {
                "hostname": relayhost.hostname,
                "username": relayhost.username or "",
                "password": relayhost.password or "",
                "active": relayhost.active,
            }
            for relayhost in bundle.relayhosts
        },
        transports={
            transport.destination: {
                "destination": transport.destination,
                "nexthop": transport.nexthop,
                "username": transport.username or "",
                "password": transport.password or "",
                "active": transport.active,
            }
            for transport in bundle.transports
        },
        domain_admins={
            admin.username: {
                "username": admin.username,
                "domains": sorted(admin.domains),
                "active": admin.active,
            }
            for admin in bundle.domain_admins
        },
    )


def desired_from_local(client: MailcowClient) -> DesiredState:
    return desired_from_bundle(export_bundle(client))


# ---------------------------------------------------------------------- payloads


def _domain_payload(domain: str, attrs: dict) -> dict:
    return {
        "domain": domain,
        "description": attrs.get("description") or "",
        "active": int(bool(attrs.get("active", True))),
        "aliases": int(attrs.get("aliases") or 0),
        "mailboxes": int(attrs.get("mailboxes") or 0),
        "quota": int(attrs.get("quota") or 0),
        "maxquota": int(attrs.get("maxquota") or 0),
        "defquota": int(attrs.get("defquota") or 0),
        "backupmx": int(bool(attrs.get("backupmx"))),
        "gal": int(bool(attrs.get("gal"))),
        "relay_all_recipients": int(bool(attrs.get("relay_all_recipients"))),
        "relay_unknown_only": int(bool(attrs.get("relay_unknown_only"))),
    }


def _domain_changed(desired: dict, actual: dict) -> bool:
    for key, (actual_key, transform) in _DOMAIN_FIELDS.items():
        if actual_key not in actual:
            continue  # field not reported by this mailcow version
        try:
            if str(transform(desired.get(key))) != str(actual.get(actual_key)):
                return True
        except (TypeError, ValueError):
            return True
    return False


def _mailbox_parts(attrs: dict) -> tuple[str, str]:
    local_part = attrs.get("local_part")
    domain = attrs.get("domain")
    if not local_part or not domain:
        local_part, _, domain = str(attrs["username"]).partition("@")
    return local_part, domain


def _mailbox_changed(desired: dict, actual: dict) -> bool:
    if "quota" in actual and int(desired.get("quota") or 0) * MIB != int(actual["quota"]):
        return True
    if "active" in actual and int(bool(desired.get("active", True))) != int(actual["active"]):
        return True
    if actual.get("name") and (desired.get("name") or "") != actual.get("name"):
        return True
    return False


# ---------------------------------------------------------------------- planning


def build_plan(
    api: MailcowApiClient,
    desired: DesiredState,
    *,
    target_url: str,
    source: str,
    prune: bool = False,
    include_mailboxes: bool = False,
    include_domain_admins: bool = False,
) -> ReconcilePlan:
    actions: list[ReconcileAction] = []

    # ---- domains
    actual_domains = {d.get("domain_name"): d for d in api.list_domains() if d.get("domain_name")}
    for domain, attrs in sorted(desired.domains.items()):
        actual = actual_domains.get(domain)
        if actual is None:
            actions.append(
                ReconcileAction(
                    kind="domain",
                    action="create",
                    target=domain,
                    detail="domain missing on target",
                    payload=_domain_payload(domain, attrs),
                )
            )
        elif _domain_changed(attrs, actual):
            actions.append(
                ReconcileAction(
                    kind="domain",
                    action="update",
                    target=domain,
                    detail="domain settings differ",
                    payload=_domain_payload(domain, attrs),
                )
            )

    # ---- dkim (only for domains we manage; skip when keys already match)
    for domain, key in sorted(desired.dkim.items()):
        if domain not in desired.domains and domain not in desired.alias_domains:
            continue
        if not key.private_key:
            continue
        actual_key = api.get_dkim(domain)
        if actual_key and actual_key.get("pubkey") == key.public_key:
            continue
        actions.append(
            ReconcileAction(
                kind="dkim",
                action="import-dkim",
                target=domain,
                detail=f"import private key (selector {key.selector or 'dkim'})",
            )
        )

    # ---- alias domains
    actual_alias_domains = {
        a.get("alias_domain"): a for a in api.list_alias_domains() if a.get("alias_domain")
    }
    for name, attrs in sorted(desired.alias_domains.items()):
        actual = actual_alias_domains.get(name)
        if actual is None:
            actions.append(
                ReconcileAction(
                    kind="alias-domain",
                    action="create",
                    target=name,
                    detail=f"alias domain -> {attrs['target_domain']}",
                    payload={
                        "alias_domain": name,
                        "target_domain": attrs["target_domain"],
                        "active": int(bool(attrs.get("active", True))),
                    },
                )
            )
        elif actual.get("target_domain") != attrs["target_domain"]:
            actions.append(
                ReconcileAction(
                    kind="alias-domain",
                    action="update",
                    target=name,
                    detail=f"retarget to {attrs['target_domain']}",
                    payload={"target_domain": attrs["target_domain"]},
                )
            )

    # ---- aliases (edits/deletes are keyed by the target's numeric id)
    actual_aliases = {
        a.get("address"): a for a in api.list_aliases() if a.get("address")
    }
    for address, attrs in sorted(desired.aliases.items()):
        actual = actual_aliases.get(address)
        if actual is None:
            actions.append(
                ReconcileAction(
                    kind="alias",
                    action="create",
                    target=address,
                    detail=f"alias -> {attrs['goto']}",
                    payload={
                        "address": address,
                        "goto": attrs["goto"],
                        "active": int(bool(attrs.get("active", True))),
                        "sogo_visible": 1,
                    },
                )
            )
        elif actual.get("goto") != attrs["goto"] or int(actual.get("active", 1)) != int(
            bool(attrs.get("active", True))
        ):
            actions.append(
                ReconcileAction(
                    kind="alias",
                    action="update",
                    target=address,
                    detail="alias target/state differs",
                    payload={
                        "id": actual.get("id"),
                        "attr": {
                            "address": address,
                            "goto": attrs["goto"],
                            "active": int(bool(attrs.get("active", True))),
                        },
                    },
                )
            )

    # ---- mailboxes (opt-in; passwords cannot be scraped)
    if include_mailboxes:
        actual_mailboxes = {
            m.get("username"): m for m in api.list_mailboxes() if m.get("username")
        }
        for username, attrs in sorted(desired.mailboxes.items()):
            actual = actual_mailboxes.get(username)
            if actual is None:
                local_part, domain = _mailbox_parts(attrs)
                actions.append(
                    ReconcileAction(
                        kind="mailbox",
                        action="create",
                        target=username,
                        detail="create with random password + force_pw_update",
                        payload={
                            "local_part": local_part,
                            "domain": domain,
                            "name": attrs.get("name") or local_part,
                            "quota": int(attrs.get("quota") or 0),
                            "active": int(bool(attrs.get("active", True))),
                            "force_pw_update": 1,
                        },
                    )
                )
            elif _mailbox_changed(attrs, actual):
                actions.append(
                    ReconcileAction(
                        kind="mailbox",
                        action="update",
                        target=username,
                        detail="mailbox settings differ",
                        payload={
                            "name": attrs.get("name") or "",
                            "quota": int(attrs.get("quota") or 0),
                            "active": int(bool(attrs.get("active", True))),
                        },
                    )
                )

    # ---- relayhosts (SMTP relay credentials; stored in the clear by mailcow)
    actual_relayhosts = {
        r.get("hostname"): r for r in api.list_relayhosts() if r.get("hostname")
    }
    for hostname, attrs in sorted(desired.relayhosts.items()):
        add_payload = {
            "hostname": hostname,
            "username": attrs.get("username") or "",
            "password": attrs.get("password") or "",
        }
        actual = actual_relayhosts.get(hostname)
        if actual is None:
            actions.append(
                ReconcileAction(
                    kind="relayhost",
                    action="create",
                    target=hostname,
                    detail="relayhost missing on target",
                    payload={"add": add_payload},
                )
            )
        elif str(actual.get("username") or "") != add_payload["username"] or str(
            actual.get("password") or ""
        ) != add_payload["password"]:
            actions.append(
                ReconcileAction(
                    kind="relayhost",
                    action="update",
                    target=hostname,
                    detail="credentials differ (no edit endpoint: delete + re-add)",
                    payload={"id": actual.get("id"), "add": add_payload},
                )
            )

    # ---- transports (sender-dependent relay credentials)
    actual_transports = {
        t.get("destination"): t for t in api.list_transports() if t.get("destination")
    }
    for destination, attrs in sorted(desired.transports.items()):
        add_payload = {
            "destination": destination,
            "nexthop": attrs.get("nexthop") or "",
            "username": attrs.get("username") or "",
            "password": attrs.get("password") or "",
            "active": int(bool(attrs.get("active", True))),
        }
        actual = actual_transports.get(destination)
        if actual is None:
            actions.append(
                ReconcileAction(
                    kind="transport",
                    action="create",
                    target=destination,
                    detail="transport missing on target",
                    payload={"add": add_payload},
                )
            )
        else:
            differs = (
                str(actual.get("nexthop") or "") != add_payload["nexthop"]
                or str(actual.get("username") or "") != add_payload["username"]
                or str(actual.get("password") or "") != add_payload["password"]
            )
            if differs:
                actions.append(
                    ReconcileAction(
                        kind="transport",
                        action="update",
                        target=destination,
                        detail="credentials differ (no edit endpoint: delete + re-add)",
                        payload={"id": actual.get("id"), "add": add_payload},
                    )
                )

    # ---- domain admins (opt-in; the account is recreated, not its password)
    actual_admins: dict[str, dict] = {}
    if include_domain_admins:
        actual_admins = {
            a.get("username"): a for a in api.list_domain_admins() if a.get("username")
        }
        for username, attrs in sorted(desired.domain_admins.items()):
            domains = sorted(attrs.get("domains") or [])
            active = int(bool(attrs.get("active", True)))
            actual = actual_admins.get(username)
            if actual is None:
                actions.append(
                    ReconcileAction(
                        kind="domain-admin",
                        action="create",
                        target=username,
                        detail="create with random password (original is unreadable)",
                        payload={
                            "username": username,
                            "domains": ",".join(domains),
                            "active": active,
                        },
                    )
                )
            elif sorted(actual.get("selected_domains") or []) != domains or int(
                actual.get("active", 1)
            ) != active:
                actions.append(
                    ReconcileAction(
                        kind="domain-admin",
                        action="update",
                        target=username,
                        detail="domain grants/state differ",
                        payload={"active": active, "domains": domains},
                    )
                )

    # ---- pruning (opt-in, conservative)
    if prune:
        for domain in sorted(set(actual_domains) - set(desired.domains)):
            actions.append(
                ReconcileAction(kind="domain", action="delete", target=domain, detail="not in desired state")
            )
        for name in sorted(set(actual_alias_domains) - set(desired.alias_domains)):
            actions.append(
                ReconcileAction(
                    kind="alias-domain", action="delete", target=name, detail="not in desired state"
                )
            )
        managed_domains = set(desired.domains) | set(desired.alias_domains)
        for address, actual in sorted(actual_aliases.items()):
            if address in desired.aliases:
                continue
            if actual.get("domain") not in managed_domains:
                continue  # leave aliases of unmanaged domains alone
            actions.append(
                ReconcileAction(
                    kind="alias",
                    action="delete",
                    target=address,
                    detail="not in desired state",
                    payload={"id": actual.get("id")},
                )
            )
        if include_mailboxes:
            for username in sorted(set(actual_mailboxes) - set(desired.mailboxes)):
                actions.append(
                    ReconcileAction(
                        kind="mailbox", action="delete", target=username, detail="not in desired state"
                    )
                )
        for hostname, actual in sorted(actual_relayhosts.items()):
            if hostname in desired.relayhosts:
                continue
            actions.append(
                ReconcileAction(
                    kind="relayhost",
                    action="delete",
                    target=hostname,
                    detail="not in desired state",
                    payload={"id": actual.get("id")},
                )
            )
        for destination, actual in sorted(actual_transports.items()):
            if destination in desired.transports:
                continue
            actions.append(
                ReconcileAction(
                    kind="transport",
                    action="delete",
                    target=destination,
                    detail="not in desired state",
                    payload={"id": actual.get("id")},
                )
            )
        if include_domain_admins:
            for username in sorted(set(actual_admins) - set(desired.domain_admins)):
                actions.append(
                    ReconcileAction(
                        kind="domain-admin",
                        action="delete",
                        target=username,
                        detail="not in desired state",
                    )
                )

    counts: dict[str, int] = {}
    for action in actions:
        counts[action.action] = counts.get(action.action, 0) + 1

    return ReconcilePlan(
        target_url=target_url,
        source=source,
        generated_at=datetime.now(timezone.utc).isoformat(),
        prune=prune,
        include_mailboxes=include_mailboxes,
        actions=actions,
        counts=counts,
    )


# ---------------------------------------------------------------------- applying


def _dispatch(api: MailcowApiClient, action: ReconcileAction, desired: DesiredState) -> None:
    payload = action.payload or {}

    if action.kind == "domain":
        if action.action == "create":
            api.add_domain(payload)
        elif action.action == "update":
            api.edit_domain(action.target, payload)
        elif action.action == "delete":
            api.delete_domain(action.target)

    elif action.kind == "dkim":
        key = desired.dkim.get(action.target)
        if key and key.private_key:
            api.import_dkim(action.target, key.selector or "dkim", key.private_key, overwrite=True)

    elif action.kind == "alias":
        if action.action == "create":
            api.add_alias(payload)
        elif action.action == "update":
            alias_id = payload["id"]
            if alias_id is not None:
                api.edit_alias(int(alias_id), payload["attr"])
        elif action.action == "delete":
            alias_id = payload.get("id")
            if alias_id is not None:
                api.delete_alias(int(alias_id))

    elif action.kind == "alias-domain":
        if action.action == "create":
            api.add_alias_domain(payload)
        elif action.action == "update":
            api.edit_alias_domain(action.target, payload)
        elif action.action == "delete":
            api.delete_alias_domain(action.target)

    elif action.kind == "mailbox":
        if action.action == "create":
            password = secrets.token_urlsafe(20)
            body = dict(payload, password=password, password2=password)
            api.add_mailbox(body)
        elif action.action == "update":
            api.edit_mailbox(action.target, payload)
        elif action.action == "delete":
            api.delete_mailbox(action.target)

    elif action.kind == "relayhost":
        if action.action == "create":
            api.add_relayhost(payload["add"])
        elif action.action == "update":
            # No edit endpoint in mailcow: replace it.
            if payload.get("id") is not None:
                api.delete_relayhost(int(payload["id"]))
            api.add_relayhost(payload["add"])
        elif action.action == "delete":
            if payload.get("id") is not None:
                api.delete_relayhost(int(payload["id"]))

    elif action.kind == "transport":
        if action.action == "create":
            api.add_transport(payload["add"])
        elif action.action == "update":
            if payload.get("id") is not None:
                api.delete_transport(int(payload["id"]))
            api.add_transport(payload["add"])
        elif action.action == "delete":
            if payload.get("id") is not None:
                api.delete_transport(int(payload["id"]))

    elif action.kind == "domain-admin":
        if action.action == "create":
            password = secrets.token_urlsafe(20)
            body = dict(payload, password=password, password2=password)
            api.add_domain_admin(body)
        elif action.action == "update":
            api.edit_domain_admin(action.target, payload)
        elif action.action == "delete":
            api.delete_domain_admin(action.target)


def apply_plan(
    api: MailcowApiClient, plan: ReconcilePlan, desired: DesiredState
) -> ReconcileResult:
    applied = 0
    errors: list[str] = []
    for action in plan.actions:
        try:
            _dispatch(api, action, desired)
            applied += 1
        except MailcowApiError as exc:
            errors.append(f"{action.kind}/{action.action} {action.target}: {exc}")
    return ReconcileResult(
        plan=plan, applied=applied, failed=len(errors), errors=errors
    )


# ---------------------------------------------------------------------- CLI


def _local_client(settings: SidecarSettings) -> MailcowClient:
    config = MailcowConfig.load(settings)
    docker = DockerClient(
        binary=settings.docker_bin,
        project=config.compose_project,
        timeout=settings.request_timeout,
        cache_ttl=settings.cache_ttl,
    )
    return MailcowClient(config, docker, cache=_NoCache())


class _NoCache:
    """Zero-TTL cache shim so the CLI always scrapes fresh."""

    ttl = 0.0

    def get_or_set(self, key: str, factory: Callable[[], Any]) -> Any:  # noqa: ARG002
        return factory()

    def invalidate(self, key: str | None = None) -> None:  # noqa: ARG002
        return None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Reconcile mailcow config to another node.")
    parser.add_argument("--target-url", default=None, help="e.g. https://mail.example.org (or SIDECAR_TARGET_URL)")
    parser.add_argument("--target-api-key", default=None, help="rw API key (or SIDECAR_TARGET_API_KEY)")
    parser.add_argument("--source", choices=["local", "bundle"], default="local")
    parser.add_argument("--bundle", help="ExportBundle JSON file (with --source bundle)")
    parser.add_argument("--include-mailboxes", action="store_true")
    parser.add_argument("--include-domain-admins", action="store_true")
    parser.add_argument("--prune", action="store_true", help="delete objects absent from desired state")
    parser.add_argument("--apply", action="store_true", help="execute (default is dry-run)")
    parser.add_argument("--insecure", action="store_true", help="skip TLS verification")
    args = parser.parse_args(argv)

    settings = SidecarSettings.from_env()
    local = _local_client(settings)

    if args.source == "bundle":
        if not args.bundle:
            parser.error("--source bundle requires --bundle")
        bundle = ExportBundle.model_validate(json.loads(open(args.bundle).read()))
        desired = desired_from_bundle(bundle)
    else:
        desired = desired_from_local(local)

    # Explicit target wins; otherwise derive this node's own mailcow target.
    target_url = args.target_url or settings.target_url
    api_key = args.target_api_key or settings.target_api_key
    verify_tls = not args.insecure
    if not (target_url and api_key):
        try:
            resolved = local.resolve_write_target(settings)
        except WriteTargetError as exc:
            parser.error(str(exc))
        target_url, api_key = resolved.url, resolved.api_key
        if not args.insecure:
            verify_tls = resolved.verify_tls

    with MailcowApiClient(
        target_url, api_key, verify_tls=verify_tls, timeout=settings.target_timeout
    ) as api:
        plan = build_plan(
            api,
            desired,
            target_url=target_url,
            source=args.source,
            prune=args.prune,
            include_mailboxes=args.include_mailboxes,
            include_domain_admins=args.include_domain_admins,
        )
        print(json.dumps(plan.model_dump(), indent=2))
        if not args.apply:
            print(f"dry-run: {len(plan.actions)} action(s); re-run with --apply", file=sys.stderr)
            return 0
        result = apply_plan(api, plan, desired)
        print(json.dumps(result.model_dump(), indent=2))
        return 1 if result.failed else 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
