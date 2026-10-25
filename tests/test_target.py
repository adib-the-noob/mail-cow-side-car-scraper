"""Tests for resolving this node's own write target (the receive path)."""

from __future__ import annotations

from app.cache import TTLCache
from app.config import MailcowConfig, SidecarSettings
from app.docker_exec import DockerClient
from app.mailcow import MailcowClient, WriteTargetError
from tests.stub_docker import DEFAULT_API_KEY


def _client(mailcow_dir, stub_docker: str) -> MailcowClient:
    settings = SidecarSettings(mailcow_dir=str(mailcow_dir))
    config = MailcowConfig.load(settings)
    docker = DockerClient(
        binary=stub_docker,
        project=config.compose_project,
        timeout=10.0,
        cache_ttl=0,
    )
    return MailcowClient(config, docker, TTLCache(0))


def test_explicit_target_wins(mailcow_dir, stub_docker: str) -> None:
    settings = SidecarSettings(
        mailcow_dir=str(mailcow_dir),
        target_url="https://node2.example.org",
        target_api_key="explicit-key",
        target_verify_tls=True,
    )
    target = _client(mailcow_dir, stub_docker).resolve_write_target(settings)
    assert (target.url, target.api_key, target.verify_tls, target.source) == (
        "https://node2.example.org",
        "explicit-key",
        True,
        "configured",
    )


def test_existing_rw_key_is_reused(mailcow_dir, stub_docker: str) -> None:
    """No config at all: derive the URL from mailcow.conf and reuse mailcow's key."""
    settings = SidecarSettings(mailcow_dir=str(mailcow_dir))
    target = _client(mailcow_dir, stub_docker).resolve_write_target(settings)
    assert target.url == "https://127.0.0.1:443"
    assert target.api_key == DEFAULT_API_KEY
    assert target.source == "existing-key"
    # Loopback cannot validate mailcow's cert (issued for MAILCOW_HOSTNAME).
    assert target.verify_tls is False


def test_key_is_created_when_mailcow_has_none(
    mailcow_dir, stub_docker: str, monkeypatch
) -> None:
    monkeypatch.setenv("STUB_API_KEY", "none")
    settings = SidecarSettings(mailcow_dir=str(mailcow_dir))
    target = _client(mailcow_dir, stub_docker).resolve_write_target(settings)

    assert target.source == "created-key"
    # mailcow's key format: 6 dash-separated groups of 6 hex characters.
    groups = target.api_key.split("-")
    assert len(groups) == 6
    assert all(len(group) == 6 for group in groups)


def test_provision_false_reports_without_creating(
    mailcow_dir, stub_docker: str, monkeypatch
) -> None:
    monkeypatch.setenv("STUB_API_KEY", "none")
    settings = SidecarSettings(mailcow_dir=str(mailcow_dir))
    target = _client(mailcow_dir, stub_docker).resolve_write_target(
        settings, provision=False
    )
    assert target.source == "auto-create"
    assert target.api_key == ""


def test_auto_disabled_without_key_raises(
    mailcow_dir, stub_docker: str, monkeypatch
) -> None:
    monkeypatch.setenv("STUB_API_KEY", "none")
    settings = SidecarSettings(mailcow_dir=str(mailcow_dir), auto_api_key=False)
    try:
        _client(mailcow_dir, stub_docker).resolve_write_target(settings)
    except WriteTargetError as exc:
        assert "no write target" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("expected WriteTargetError")
