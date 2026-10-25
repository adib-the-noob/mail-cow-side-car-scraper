"""Pure helpers for DKIM key data (mirrors mailcow's PHP logic)."""

from __future__ import annotations

# mailcow builds the TXT value from the base64 public key without PEM armour.
DKIM_TXT_PREFIX = "v=DKIM1;k=rsa;t=s;s=email;p="


def infer_key_length(public_key: str) -> str:
    """Infer the RSA key size the same way mailcow's ``dkim('details')`` does."""
    length = len(public_key)
    if length < 391:
        return "1024"
    if length < 564:
        return "2048"
    if length < 736:
        return "3072"
    if length < 1416:
        return "4096"
    return ">= 8192"


def build_dkim_txt(public_key: str, split_255: bool = False) -> str:
    """Build the DKIM TXT record value.

    When ``SPLIT_DKIM_255`` is enabled mailcow chunks the value into 255-char
    quoted strings, which some DNS providers require for long TXT records.
    """
    full = DKIM_TXT_PREFIX + public_key
    if not split_255:
        return full
    chunks = [full[i : i + 255] for i in range(0, len(full), 255)]
    return '"' + '" "'.join(chunks) + '"'
