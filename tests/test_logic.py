"""Unit tests for the pure logic (no Docker involved)."""

from __future__ import annotations

from pathlib import Path

from app.config import parse_mailcow_conf
from app.dkim import build_dkim_txt, infer_key_length
from app.dns import build_dns_records
from app.models import DkimKey


def test_infer_key_length() -> None:
    assert infer_key_length("A" * 200) == "1024"
    assert infer_key_length("A" * 450) == "2048"
    assert infer_key_length("A" * 600) == "3072"
    assert infer_key_length("A" * 1000) == "4096"
    assert infer_key_length("A" * 2000) == ">= 8192"


def test_build_dkim_txt_unsplit() -> None:
    assert build_dkim_txt("PUBKEY") == "v=DKIM1;k=rsa;t=s;s=email;p=PUBKEY"


def test_build_dkim_txt_split_255() -> None:
    value = build_dkim_txt("P" * 300, split_255=True)
    assert value.startswith('"v=DKIM1;k=rsa;t=s;s=email;p=')
    assert value.endswith('"')
    chunks = value[1:-1].split('" "')
    assert all(len(chunk) <= 255 for chunk in chunks)
    assert "".join(chunks) == "v=DKIM1;k=rsa;t=s;s=email;p=" + "P" * 300


def test_parse_mailcow_conf(tmp_path: Path) -> None:
    conf = tmp_path / "mailcow.conf"
    conf.write_text(
        "# comment\n"
        "MAILCOW_HOSTNAME=mail.example.org\n"
        'QUOTED="value with spaces"\n'
        "\n"
        "REDISPASS=abc123\n"
    )
    parsed = parse_mailcow_conf(conf)
    assert parsed["MAILCOW_HOSTNAME"] == "mail.example.org"
    assert parsed["QUOTED"] == "value with spaces"
    assert parsed["REDISPASS"] == "abc123"
    assert "# comment" not in parsed


def _dkim() -> DkimKey:
    return DkimKey(
        domain="example.org",
        selector="dkim",
        length="2048",
        public_key="PUB",
        private_key="PRIV",
        dkim_txt="v=DKIM1;k=rsa;t=s;s=email;p=PUB",
    )


def test_build_dns_records_core() -> None:
    records = build_dns_records(
        "example.org", hostname="mail.example.org", https_port=443, dkim=_dkim()
    )
    by_key = {(r.name, r.type): r for r in records}

    assert by_key[("example.org", "MX")].value == "10 mail.example.org."
    assert by_key[("example.org", "TXT")].value == "v=spf1 mx a -all"
    assert by_key[("_dmarc.example.org", "TXT")].value.startswith("v=DMARC1")
    assert by_key[("dkim._domainkey.example.org", "TXT")].value.endswith("p=PUB")
    assert by_key[("autodiscover.example.org", "CNAME")].value == "mail.example.org."
    assert by_key[("_imaps._tcp.example.org", "SRV")].value == "0 1 993 mail.example.org."

    mx = by_key[("example.org", "MX")]
    assert mx.mandatory is True


def test_build_dns_records_alias_domain_skips_autoconfig() -> None:
    records = build_dns_records(
        "alias.example.net",
        hostname="mail.example.org",
        dkim=None,
        is_alias_domain=True,
    )
    names = {r.name for r in records}
    assert "alias.example.net" in names
    assert "autodiscover.alias.example.net" not in names


def test_build_dns_records_without_srv() -> None:
    records = build_dns_records(
        "example.org", hostname="mail.example.org", dkim=None, include_srv=False
    )
    assert all(r.type != "SRV" or r.name.startswith("_autodiscover") for r in records)
