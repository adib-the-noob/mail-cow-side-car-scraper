"""Compose scraped rows into API models."""

from __future__ import annotations

from datetime import datetime, timezone

from .dns import build_dns_records
from .mailcow import MailcowClient
from .models import (
    AliasDomainInfo,
    AliasInfo,
    DkimKey,
    DomainInfo,
    DnsRecord,
    ExportBundle,
    MailboxInfo,
)


def _to_bool(value: object, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "y", "on"}


def _to_int(value: object) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _alias_domain_set(client: MailcowClient) -> set[str]:
    return {row["alias_domain"] for row in client.alias_domains() if row.get("alias_domain")}


def build_domain_info(
    client: MailcowClient,
    row: dict,
    dkim_map: dict[str, DkimKey],
    alias_domains: set[str],
    *,
    include_dns: bool = True,
) -> DomainInfo:
    domain = row["domain"]
    dkim = dkim_map.get(domain)
    info = DomainInfo(
        domain=domain,
        active=_to_bool(row.get("active"), True),
        description=row.get("description"),
        aliases=_to_int(row.get("aliases")),
        mailboxes=_to_int(row.get("mailboxes")),
        defquota=_to_int(row.get("defquota")),
        maxquota=_to_int(row.get("maxquota")),
        quota=_to_int(row.get("quota")),
        relayhost=row.get("relayhost"),
        backupmx=_to_bool(row.get("backupmx")),
        gal=_to_bool(row.get("gal")),
        relay_all_recipients=_to_bool(row.get("relay_all_recipients")),
        relay_unknown_only=_to_bool(row.get("relay_unknown_only")),
        is_alias_domain=domain in alias_domains,
        created=row.get("created"),
        modified=row.get("modified"),
        dkim=dkim,
    )
    if include_dns:
        info.dns = build_dns_records(
            domain,
            hostname=client.config.hostname,
            https_port=client.config.https_port,
            dkim=dkim,
            is_alias_domain=info.is_alias_domain,
        )
    return info


def collect_domains(client: MailcowClient, *, include_dns: bool = True) -> list[DomainInfo]:
    dkim_map = client.dkim_keys()
    alias_domains = _alias_domain_set(client)
    rows = client.domains()
    infos = [
        build_domain_info(client, row, dkim_map, alias_domains, include_dns=include_dns)
        for row in rows
    ]
    return sorted(infos, key=lambda info: info.domain)


def collect_alias_domains(client: MailcowClient) -> list[AliasDomainInfo]:
    return [
        AliasDomainInfo(
            alias_domain=row["alias_domain"],
            target_domain=row["target_domain"],
            active=_to_bool(row.get("active"), True),
        )
        for row in client.alias_domains()
    ]


def collect_dkim(client: MailcowClient) -> list[DkimKey]:
    return sorted(client.dkim_keys().values(), key=lambda key: key.domain)


def collect_dns(client: MailcowClient, *, include_srv: bool = True) -> dict[str, list[DnsRecord]]:
    dkim_map = client.dkim_keys()
    alias_domains = _alias_domain_set(client)
    result: dict[str, list[DnsRecord]] = {}
    for row in client.domains():
        domain = row["domain"]
        result[domain] = build_dns_records(
            domain,
            hostname=client.config.hostname,
            https_port=client.config.https_port,
            dkim=dkim_map.get(domain),
            is_alias_domain=domain in alias_domains,
            include_srv=include_srv,
        )
    return result


def collect_mailboxes(client: MailcowClient, domain: str | None = None) -> list[MailboxInfo]:
    rows = client.mailboxes()
    if domain:
        wanted = domain.strip().lower()
        rows = [row for row in rows if str(row.get("domain", "")).lower() == wanted]
    return [
        MailboxInfo(
            username=row["username"],
            name=row.get("name"),
            domain=row["domain"],
            local_part=row.get("local_part"),
            quota=_to_int(row.get("quota")),
            active=_to_bool(row.get("active"), True),
            authsource=row.get("authsource"),
            created=row.get("created"),
            modified=row.get("modified"),
        )
        for row in rows
    ]


def collect_aliases(client: MailcowClient, domain: str | None = None) -> list[AliasInfo]:
    rows = client.aliases()
    if domain:
        wanted = domain.strip().lower()
        rows = [row for row in rows if str(row.get("domain", "")).lower() == wanted]
    return [
        AliasInfo(
            id=_to_int(row.get("id")),
            address=row["address"],
            goto=row["goto"],
            domain=row["domain"],
            active=_to_bool(row.get("active"), True),
        )
        for row in rows
    ]


def domain_bundle(client: MailcowClient, domain: str) -> DomainInfo | None:
    domain = domain.strip().lower()
    dkim_map = client.dkim_keys()
    alias_domains = _alias_domain_set(client)
    for row in client.domains():
        if row["domain"].lower() == domain:
            return build_domain_info(client, row, dkim_map, alias_domains, include_dns=True)
    # Domain exists only as a DKIM key holder (e.g. orphaned key) -- still report it.
    if domain in dkim_map:
        return DomainInfo(domain=domain, is_alias_domain=domain in alias_domains, dkim=dkim_map[domain])
    return None


def export_bundle(client: MailcowClient) -> ExportBundle:
    return ExportBundle(
        generated_at=datetime.now(timezone.utc).isoformat(),
        mailcow_hostname=client.config.hostname,
        domains=collect_domains(client, include_dns=True),
        alias_domains=collect_alias_domains(client),
    )
