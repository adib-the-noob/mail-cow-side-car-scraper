"""DNS record generation.

mailcow does not store DNS records anywhere -- its UI computes them from the
mailcow hostname, the domain and the DKIM public key. This module mirrors that
output (see ``data/web/inc/ajax/dns_diagnostics.php`` and the official DNS
setup guide) so the sidecar can hand out a ready-to-paste record set.
"""

from __future__ import annotations

from .models import DkimKey, DnsRecord

DEFAULT_SPF = "v=spf1 mx a -all"

# (service, port) pairs mailcow advertises via SRV records.
_SRV_SERVICES: tuple[tuple[str, int], ...] = (
    ("_imap._tcp", 143),
    ("_imaps._tcp", 993),
    ("_pop3._tcp", 110),
    ("_pop3s._tcp", 995),
    ("_submission._tcp", 587),
    ("_submissions._tcp", 465),
    ("_sieve._tcp", 4190),
    ("_caldavs._tcp", 443),
    ("_carddavs._tcp", 443),
)


def default_dmarc(domain: str) -> str:
    return f"v=DMARC1; p=quarantine; rua=mailto:postmaster@{domain}"


def _fqdn(hostname: str) -> str:
    """Zone-file targets are conventionally absolute (trailing dot)."""
    return hostname if hostname.endswith(".") else f"{hostname}."


def build_dns_records(
    domain: str,
    *,
    hostname: str,
    https_port: int = 443,
    dkim: DkimKey | None = None,
    is_alias_domain: bool = False,
    spf: str | None = None,
    dmarc: str | None = None,
    include_srv: bool = True,
) -> list[DnsRecord]:
    records: list[DnsRecord] = []

    # Mandatory: mail routing.
    records.append(
        DnsRecord(name=domain, type="MX", value=f"10 {_fqdn(hostname)}", mandatory=True)
    )

    # Recommended: sender authentication.
    records.append(
        DnsRecord(name=domain, type="TXT", value=spf or DEFAULT_SPF, mandatory=False)
    )
    records.append(
        DnsRecord(
            name=f"_dmarc.{domain}",
            type="TXT",
            value=dmarc or default_dmarc(domain),
            mandatory=False,
        )
    )

    if dkim is not None:
        records.append(
            DnsRecord(
                name=f"{dkim.selector or 'dkim'}._domainkey.{domain}",
                type="TXT",
                value=dkim.dkim_txt,
                mandatory=False,
            )
        )

    # Client autoconfiguration is only relevant on real (non-alias) domains.
    if not is_alias_domain:
        records.append(
            DnsRecord(
                name=f"autodiscover.{domain}",
                type="CNAME",
                value=_fqdn(hostname),
                mandatory=False,
            )
        )
        records.append(
            DnsRecord(
                name=f"autoconfig.{domain}",
                type="CNAME",
                value=_fqdn(hostname),
                mandatory=False,
            )
        )
        records.append(
            DnsRecord(
                name=f"_autodiscover._tcp.{domain}",
                type="SRV",
                value=f"0 1 {https_port} {_fqdn(hostname)}",
                mandatory=False,
            )
        )

        if include_srv:
            for service, port in _SRV_SERVICES:
                records.append(
                    DnsRecord(
                        name=f"{service}.{domain}",
                        type="SRV",
                        value=f"0 1 {port} {_fqdn(hostname)}",
                        mandatory=False,
                    )
                )

    return records
