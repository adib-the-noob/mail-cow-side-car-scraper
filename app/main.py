"""FastAPI application exposing scraped mailcow domain / DKIM / DNS data."""

from __future__ import annotations

import threading
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, HTTPException, Query, status

from . import __version__
from .cache import TTLCache
from .config import MYSQL_SERVICE, REDIS_SERVICE, RSPAMD_SERVICE, PHP_SERVICE, MailcowConfig, SidecarSettings
from .docker_exec import DockerClient, DockerError
from .mailcow import MailcowClient
from .models import (
    AliasDomainInfo,
    AliasInfo,
    DkimKey,
    DomainInfo,
    DnsRecord,
    ExportBundle,
    MailboxInfo,
    ServiceStatus,
    StatusResponse,
)
from .security import build_auth_dependency
from .service import (
    collect_alias_domains,
    collect_aliases,
    collect_dkim,
    collect_dns,
    collect_domains,
    collect_mailboxes,
    domain_bundle,
    export_bundle,
)

_MONITORED_SERVICES = (REDIS_SERVICE, MYSQL_SERVICE, PHP_SERVICE, RSPAMD_SERVICE)


class AppState:
    """Lazily resolves the mailcow install; retries until it succeeds."""

    def __init__(self, settings: SidecarSettings) -> None:
        self.settings = settings
        self.cache = TTLCache(settings.cache_ttl)
        self.config: MailcowConfig | None = None
        self.client: MailcowClient | None = None
        self.error: str | None = None
        self._lock = threading.Lock()

    def ensure(self) -> MailcowClient:
        if self.client is not None:
            return self.client
        with self._lock:
            if self.client is not None:
                return self.client
            try:
                config = MailcowConfig.load(self.settings)
                docker = DockerClient(
                    binary=self.settings.docker_bin,
                    project=config.compose_project,
                    timeout=self.settings.request_timeout,
                    cache_ttl=self.settings.cache_ttl,
                )
                self.config = config
                self.client = MailcowClient(config, docker, self.cache)
                self.error = None
            except Exception as exc:  # noqa: BLE001 - surfaced to the caller
                self.error = str(exc)
        if self.client is None:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail=f"mailcow sidecar not ready: {self.error}",
            )
        return self.client


def create_app(settings: SidecarSettings | None = None) -> FastAPI:
    settings = settings or SidecarSettings.from_env()
    state = AppState(settings)
    auth = build_auth_dependency(settings.api_token)
    guarded = [Depends(auth)]

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        # Best-effort warm-up so /health is meaningful immediately.
        try:
            state.ensure()
        except HTTPException:
            pass
        yield

    app = FastAPI(
        title="mailcow scraper sidecar",
        version=__version__,
        description=(
            "Reads mailcow domain metadata, DKIM keys (public & private) and "
            "generated DNS records straight from the mailcow containers."
        ),
        lifespan=lifespan,
    )
    app.state.app_state = state

    def get_client() -> MailcowClient:
        return state.ensure()

    # ------------------------------------------------------------------ meta

    @app.get("/", tags=["meta"])
    def root() -> dict:
        return {
            "name": "mailcow scraper sidecar",
            "version": __version__,
            "docs": "/docs",
            "endpoints": [
                "/health",
                "/api/v1/status",
                "/api/v1/domains",
                "/api/v1/domains/{domain}",
                "/api/v1/dkim",
                "/api/v1/dkim/{domain}",
                "/api/v1/dns",
                "/api/v1/dns/{domain}",
                "/api/v1/mailboxes",
                "/api/v1/aliases",
                "/api/v1/alias-domains",
                "/api/v1/export",
            ],
        }

    @app.get("/health", tags=["meta"])
    def health() -> dict:
        try:
            client = state.ensure()
        except HTTPException as exc:
            return {"status": "degraded", "ready": False, "detail": exc.detail}

        services = _service_statuses(client.docker)
        return {
            "status": "ok" if all(s.ok for s in services) else "degraded",
            "ready": True,
            "mailcow_dir": str(client.config.install_dir),
            "compose_project": client.config.compose_project,
            "services": [s.model_dump() for s in services],
        }

    # ------------------------------------------------------------------ status

    @app.get("/api/v1/status", response_model=StatusResponse, dependencies=guarded, tags=["meta"])
    def api_status(client: MailcowClient = Depends(get_client)) -> StatusResponse:
        config = client.config
        return StatusResponse(
            version=__version__,
            mailcow_dir=str(config.install_dir),
            mailcow_hostname=config.hostname,
            compose_project=config.compose_project,
            split_dkim_255=config.split_dkim_255,
            show_dkim_priv_keys=config.show_dkim_priv_keys,
            services=_service_statuses(client.docker),
        )

    @app.post("/api/v1/refresh", dependencies=guarded, tags=["meta"])
    def refresh(client: MailcowClient = Depends(get_client)) -> dict:
        client.invalidate_cache()
        return {"status": "ok", "detail": "cache cleared"}

    # ------------------------------------------------------------------ domains

    @app.get("/api/v1/domains", response_model=list[DomainInfo], dependencies=guarded, tags=["domains"])
    def list_domains(
        include_dns: bool = Query(True, description="Include generated DNS records"),
        client: MailcowClient = Depends(get_client),
    ) -> list[DomainInfo]:
        return collect_domains(client, include_dns=include_dns)

    @app.get(
        "/api/v1/domains/{domain}",
        response_model=DomainInfo,
        dependencies=guarded,
        tags=["domains"],
    )
    def get_domain(domain: str, client: MailcowClient = Depends(get_client)) -> DomainInfo:
        info = domain_bundle(client, domain)
        if info is None:
            raise HTTPException(status_code=404, detail=f"domain not found: {domain}")
        return info

    @app.get("/api/v1/alias-domains", response_model=list[AliasDomainInfo], dependencies=guarded, tags=["domains"])
    def list_alias_domains(client: MailcowClient = Depends(get_client)) -> list[AliasDomainInfo]:
        return collect_alias_domains(client)

    # ------------------------------------------------------------------ dkim

    @app.get("/api/v1/dkim", response_model=list[DkimKey], dependencies=guarded, tags=["dkim"])
    def list_dkim(client: MailcowClient = Depends(get_client)) -> list[DkimKey]:
        return collect_dkim(client)

    @app.get("/api/v1/dkim/{domain}", response_model=DkimKey, dependencies=guarded, tags=["dkim"])
    def get_dkim(domain: str, client: MailcowClient = Depends(get_client)) -> DkimKey:
        key = client.dkim_for(domain.strip().lower())
        if key is None:
            raise HTTPException(status_code=404, detail=f"no DKIM key for domain: {domain}")
        return key

    # ------------------------------------------------------------------ dns

    @app.get("/api/v1/dns", response_model=dict[str, list[DnsRecord]], dependencies=guarded, tags=["dns"])
    def list_dns(
        include_srv: bool = Query(True, description="Include the SRV record set"),
        client: MailcowClient = Depends(get_client),
    ) -> dict[str, list[DnsRecord]]:
        return collect_dns(client, include_srv=include_srv)

    @app.get("/api/v1/dns/{domain}", response_model=list[DnsRecord], dependencies=guarded, tags=["dns"])
    def get_dns(
        domain: str,
        include_srv: bool = Query(True, description="Include the SRV record set"),
        client: MailcowClient = Depends(get_client),
    ) -> list[DnsRecord]:
        records = collect_dns(client, include_srv=include_srv).get(domain.strip().lower())
        if records is None:
            raise HTTPException(status_code=404, detail=f"domain not found: {domain}")
        return records

    # ------------------------------------------------------------------ mailboxes / aliases

    @app.get("/api/v1/mailboxes", response_model=list[MailboxInfo], dependencies=guarded, tags=["mailboxes"])
    def list_mailboxes(
        domain: str | None = Query(None, description="Filter by domain"),
        client: MailcowClient = Depends(get_client),
    ) -> list[MailboxInfo]:
        return collect_mailboxes(client, domain)

    @app.get("/api/v1/aliases", response_model=list[AliasInfo], dependencies=guarded, tags=["aliases"])
    def list_aliases(
        domain: str | None = Query(None, description="Filter by domain"),
        client: MailcowClient = Depends(get_client),
    ) -> list[AliasInfo]:
        return collect_aliases(client, domain)

    # ------------------------------------------------------------------ export

    @app.get("/api/v1/export", response_model=ExportBundle, dependencies=guarded, tags=["export"])
    def export_all(client: MailcowClient = Depends(get_client)) -> ExportBundle:
        return export_bundle(client)

    @app.get(
        "/api/v1/domains/{domain}/export",
        response_model=DomainInfo,
        dependencies=guarded,
        tags=["export"],
    )
    def export_domain(domain: str, client: MailcowClient = Depends(get_client)) -> DomainInfo:
        return get_domain(domain, client)

    return app


def _service_statuses(docker: DockerClient) -> list[ServiceStatus]:
    statuses: list[ServiceStatus] = []
    for service in _MONITORED_SERVICES:
        try:
            container_id = docker.container_id(service)
            statuses.append(ServiceStatus(service=service, container_id=container_id, ok=True))
        except DockerError as exc:
            statuses.append(ServiceStatus(service=service, ok=False, error=str(exc)))
    return statuses


app = create_app()


if __name__ == "__main__":  # pragma: no cover
    import uvicorn

    _settings = SidecarSettings.from_env()
    uvicorn.run(app, host=_settings.host, port=_settings.port)
