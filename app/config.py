"""Configuration and mailcow auto-discovery.

Two layers of configuration:

* :class:`SidecarSettings` -- how the HTTP server runs, taken from the
  environment (``SIDECAR_*`` variables / ``.env``).
* :class:`MailcowConfig` -- everything we need to talk to a running mailcow
  install, taken from ``mailcow.conf``.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

# Compose service names. mailcow names its services ``<name>-mailcow`` and does
# not pin ``container_name``, so we resolve the container via compose labels.
REDIS_SERVICE = "redis-mailcow"
MYSQL_SERVICE = "mysql-mailcow"
PHP_SERVICE = "php-fpm-mailcow"
RSPAMD_SERVICE = "rspamd-mailcow"

DEFAULT_MAILCOW_DIRS = (
    "/opt/mailcow-dockerized",
    "/opt/mailcow",
    "/srv/mailcow-dockerized",
)


def _as_bool(value: str | None, default: bool = False) -> bool:
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "y", "on"}


def _as_int(value: str | None, default: int) -> int:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return default


def parse_mailcow_conf(path: Path) -> dict[str, str]:
    """Parse a mailcow.conf (simple ``KEY=value`` file) into a dict."""
    conf: dict[str, str] = {}
    for raw_line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        if key:
            conf[key] = value
    return conf


def locate_mailcow_dir(explicit: str | None = None) -> Path:
    """Return the first candidate directory that actually holds a mailcow.conf."""
    candidates: list[Path] = []
    for candidate in (explicit, os.getenv("MAILCOW_DIR")):
        if candidate:
            candidates.append(Path(candidate).expanduser())
    candidates.extend(Path(p) for p in DEFAULT_MAILCOW_DIRS)

    for candidate in candidates:
        if (candidate / "mailcow.conf").is_file():
            return candidate

    searched = ", ".join(str(c) for c in candidates)
    raise FileNotFoundError(
        f"could not find mailcow.conf (searched: {searched}). "
        "Set SIDECAR_MAILCOW_DIR or MAILCOW_DIR."
    )


@dataclass
class SidecarSettings:
    """Settings for the sidecar process itself (environment driven)."""

    host: str = "127.0.0.1"
    port: int = 8800
    docker_bin: str = "docker"
    request_timeout: float = 30.0
    cache_ttl: float = 30.0
    mailcow_dir: str | None = None
    compose_project: str | None = None
    # Reconciler target (another mailcow node). Optional; enables the write side.
    target_url: str | None = None
    target_api_key: str | None = None
    target_verify_tls: bool = True
    target_timeout: float = 30.0
    # When no target is configured, derive it from mailcow.conf and reuse (or
    # create) an rw API key in this node's own mailcow.
    auto_api_key: bool = True
    # Replication peer defaults (a peer is another sidecar).
    peer_verify_tls: bool = True
    peer_timeout: float = 120.0

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> "SidecarSettings":
        env = env if env is not None else dict(os.environ)
        return cls(
            host=env.get("SIDECAR_HOST", "127.0.0.1"),
            port=_as_int(env.get("SIDECAR_PORT"), 8800),
            docker_bin=env.get("SIDECAR_DOCKER_BIN", "docker"),
            request_timeout=float(env.get("SIDECAR_REQUEST_TIMEOUT", "30")),
            cache_ttl=float(env.get("SIDECAR_CACHE_TTL", "30")),
            mailcow_dir=env.get("SIDECAR_MAILCOW_DIR") or env.get("MAILCOW_DIR"),
            compose_project=env.get("SIDECAR_COMPOSE_PROJECT") or env.get("COMPOSE_PROJECT_NAME"),
            target_url=env.get("SIDECAR_TARGET_URL") or None,
            target_api_key=env.get("SIDECAR_TARGET_API_KEY") or None,
            target_verify_tls=_as_bool(env.get("SIDECAR_TARGET_VERIFY_TLS"), True),
            target_timeout=float(env.get("SIDECAR_TARGET_TIMEOUT", "30")),
            auto_api_key=_as_bool(env.get("SIDECAR_AUTO_API_KEY"), True),
            peer_verify_tls=_as_bool(env.get("SIDECAR_PEER_VERIFY_TLS"), True),
            peer_timeout=float(env.get("SIDECAR_PEER_TIMEOUT", "120")),
        )


@dataclass
class MailcowConfig:
    """Resolved mailcow install details."""

    install_dir: Path
    conf_file: Path
    hostname: str
    compose_project: str
    dbname: str
    dbuser: str
    dbpass: str
    dbroot: str
    redispass: str
    https_port: int
    http_port: int
    split_dkim_255: bool
    show_dkim_priv_keys: bool
    skip_sogo: bool
    raw: dict[str, str] = field(default_factory=dict)

    @classmethod
    def load(cls, settings: SidecarSettings) -> "MailcowConfig":
        install_dir = locate_mailcow_dir(settings.mailcow_dir)
        conf_file = install_dir / "mailcow.conf"
        raw = parse_mailcow_conf(conf_file)
        return cls(
            install_dir=install_dir,
            conf_file=conf_file,
            hostname=raw.get("MAILCOW_HOSTNAME", ""),
            compose_project=settings.compose_project
            or raw.get("COMPOSE_PROJECT_NAME", "mailcowdockerized"),
            dbname=raw.get("DBNAME", "mailcow"),
            dbuser=raw.get("DBUSER", "mailcow"),
            dbpass=raw.get("DBPASS", ""),
            dbroot=raw.get("DBROOT", ""),
            redispass=raw.get("REDISPASS", ""),
            https_port=_as_int(raw.get("HTTPS_PORT"), 443),
            http_port=_as_int(raw.get("HTTP_PORT"), 80),
            split_dkim_255=_as_bool(raw.get("SPLIT_DKIM_255")),
            show_dkim_priv_keys=_as_bool(raw.get("SHOW_DKIM_PRIV_KEYS")),
            skip_sogo=_as_bool(raw.get("SKIP_SOGO")),
            raw=raw,
        )
