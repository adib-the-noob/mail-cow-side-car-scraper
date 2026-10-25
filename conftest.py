"""Shared pytest fixtures.

Boots the app against a stub `docker` binary and a temporary mailcow.conf so we
can test the whole pipeline without a real mailcow install.
"""

from __future__ import annotations

import stat
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.config import SidecarSettings  # noqa: E402
from app.main import create_app  # noqa: E402

STUB_DOCKER = ROOT / "tests" / "stub_docker.py"

MAILCOW_CONF = """\
MAILCOW_HOSTNAME=mail.example.org
DBNAME=mailcow
DBUSER=mailcow
DBPASS=dbpass
DBROOT=rootpass
REDISPASS=redispass
COMPOSE_PROJECT_NAME=mailcowdockerized
HTTPS_PORT=443
HTTP_PORT=80
SPLIT_DKIM_255=false
"""


@pytest.fixture
def stub_docker() -> str:
    STUB_DOCKER.chmod(STUB_DOCKER.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return str(STUB_DOCKER)


@pytest.fixture
def mailcow_dir(tmp_path: Path) -> Path:
    (tmp_path / "mailcow.conf").write_text(MAILCOW_CONF)
    return tmp_path


def _settings(mailcow_dir: Path, stub_docker: str, token: str | None = None) -> SidecarSettings:
    return SidecarSettings(
        mailcow_dir=str(mailcow_dir),
        docker_bin=stub_docker,
        cache_ttl=0,
        api_token=token,
    )


@pytest.fixture
def client(mailcow_dir: Path, stub_docker: str) -> TestClient:
    app = create_app(_settings(mailcow_dir, stub_docker))
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture
def authed_client(mailcow_dir: Path, stub_docker: str) -> TestClient:
    app = create_app(_settings(mailcow_dir, stub_docker, token="s3cret"))
    with TestClient(app) as test_client:
        yield test_client
