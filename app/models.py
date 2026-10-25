"""Pydantic response models."""

from __future__ import annotations

from pydantic import BaseModel, Field


class DkimKey(BaseModel):
    domain: str
    selector: str | None = None
    length: str | None = None
    public_key: str = Field(description="base64 DKIM public key (Redis DKIM_PUB_KEYS)")
    private_key: str | None = Field(
        default=None,
        description="PEM private key (Redis DKIM_PRIV_KEYS); null when none is stored",
    )
    dkim_txt: str = Field(description="ready-to-publish DKIM TXT record value")


class DnsRecord(BaseModel):
    name: str
    type: str
    value: str
    ttl: int | None = None
    mandatory: bool = False


class DomainInfo(BaseModel):
    domain: str
    active: bool = True
    description: str | None = None
    aliases: int | None = None
    mailboxes: int | None = None
    defquota: int | None = None
    maxquota: int | None = None
    quota: int | None = None
    relayhost: str | None = None
    backupmx: bool = False
    gal: bool = False
    relay_all_recipients: bool = False
    relay_unknown_only: bool = False
    is_alias_domain: bool = False
    created: str | None = None
    modified: str | None = None
    dkim: DkimKey | None = None
    dns: list[DnsRecord] | None = None


class MailboxInfo(BaseModel):
    username: str
    name: str | None = None
    domain: str
    local_part: str | None = None
    quota: int | None = None
    active: bool = True
    authsource: str | None = None
    created: str | None = None
    modified: str | None = None


class AliasInfo(BaseModel):
    id: int | None = None
    address: str
    goto: str
    domain: str
    active: bool = True


class AliasDomainInfo(BaseModel):
    alias_domain: str
    target_domain: str
    active: bool = True


class ServiceStatus(BaseModel):
    service: str
    container_id: str | None = None
    ok: bool = False
    error: str | None = None


class StatusResponse(BaseModel):
    version: str
    mailcow_dir: str
    mailcow_hostname: str
    compose_project: str
    split_dkim_255: bool
    show_dkim_priv_keys: bool
    services: list[ServiceStatus]


class ExportBundle(BaseModel):
    generated_at: str
    mailcow_hostname: str
    domains: list[DomainInfo]
    alias_domains: list[AliasDomainInfo]
