"""Pydantic response models."""

from __future__ import annotations

from typing import Literal

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
    """A complete, portable snapshot of a mailcow instance's configuration."""

    generated_at: str
    mailcow_hostname: str
    domains: list[DomainInfo]
    alias_domains: list[AliasDomainInfo]
    mailboxes: list[MailboxInfo] = Field(default_factory=list)
    aliases: list[AliasInfo] = Field(default_factory=list)


ActionKind = Literal["domain", "dkim", "alias", "alias-domain", "mailbox"]
ActionType = Literal["create", "update", "delete", "import-dkim"]


class ReconcileAction(BaseModel):
    kind: ActionKind
    action: ActionType
    target: str
    detail: str
    payload: dict | None = Field(
        default=None, description="Non-secret API body; DKIM private keys are never included"
    )


class ReconcilePlan(BaseModel):
    target_url: str
    source: str = Field(description='"local" (scraped here) or "bundle" (supplied)')
    generated_at: str
    prune: bool
    include_mailboxes: bool
    actions: list[ReconcileAction]
    counts: dict[str, int]


class ReconcileResult(BaseModel):
    plan: ReconcilePlan
    applied: int
    failed: int
    errors: list[str] = Field(default_factory=list)


class ReplicateRequest(BaseModel):
    """One route, two directions.

    * **send** — set ``peer`` to push this node's configuration to another sidecar.
    * **receive** — post a ``bundle`` to apply it to this node's own mailcow.
    """

    peer: str | None = Field(
        default=None,
        description="Peer to push to, e.g. 10.0.0.2:8800 or https://10.0.0.2:8800",
    )
    verify_tls: bool | None = Field(
        default=None, description="Override TLS verification for this push"
    )
    bundle: ExportBundle | None = Field(
        default=None,
        description="Send mode: override the source bundle (defaults to scraping this node). "
        "Receive mode: the configuration to apply locally.",
    )
    prune: bool = Field(default=False, description="Also delete what is absent from the source")
    include_mailboxes: bool = Field(
        default=False, description="Include mailboxes (random password + force_pw_update)"
    )


class ReplicateResponse(BaseModel):
    mode: Literal["send", "receive"]
    peer: str | None = None
    domains: int | None = None
    mailboxes: int | None = None
    aliases: int | None = None
    applied: int = 0
    failed: int = 0
    errors: list[str] = Field(default_factory=list)
