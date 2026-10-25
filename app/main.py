"""FastAPI application exposing scraped mailcow domain / DKIM / DNS data."""

from __future__ import annotations

import threading
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, HTTPException, Query, status

from . import __version__
from .cache import TTLCache
from .config import MYSQL_SERVICE, REDIS_SERVICE, RSPAMD_SERVICE, PHP_SERVICE, MailcowConfig, SidecarSettings
from .docker_exec import DockerClient, DockerError
from .mailcow import MailcowClient, WriteTargetError
from .mailcow_api import MailcowApiClient, MailcowApiError
from .models import (
    AliasDomainInfo,
    AliasInfo,
    DkimKey,
    DomainInfo,
    DnsRecord,
    ExportBundle,
    MailboxInfo,
    ReplicateRequest,
    ReplicateResponse,
    ServiceStatus,
    StatusResponse,
    WriteTargetStatus,
)
from .reconcile import apply_plan, build_plan, desired_from_bundle
from .replicate import PeerError, send_bundle
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

    def local_write_client(client: MailcowClient) -> MailcowApiClient:
        """A client for this node's own mailcow (auto-derived when unconfigured)."""
        try:
            target = client.resolve_write_target(settings)
        except WriteTargetError as exc:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)
            ) from exc
        return MailcowApiClient(
            target.url,
            target.api_key,
            verify_tls=target.verify_tls,
            timeout=settings.target_timeout,
        )

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
                "/api/v1/replicate",
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

    @app.get("/api/v1/status", response_model=StatusResponse, tags=["meta"])
    def api_status(client: MailcowClient = Depends(get_client)) -> StatusResponse:
        config = client.config
        try:
            target = client.resolve_write_target(settings, provision=False)
            write_target = WriteTargetStatus(url=target.url, source=target.source)
        except WriteTargetError:
            write_target = WriteTargetStatus(source="unavailable")
        return StatusResponse(
            version=__version__,
            mailcow_dir=str(config.install_dir),
            mailcow_hostname=config.hostname,
            compose_project=config.compose_project,
            split_dkim_255=config.split_dkim_255,
            show_dkim_priv_keys=config.show_dkim_priv_keys,
            services=_service_statuses(client.docker),
            write_target=write_target,
        )

    @app.post("/api/v1/refresh", tags=["meta"])
    def refresh(client: MailcowClient = Depends(get_client)) -> dict:
        client.invalidate_cache()
        return {"status": "ok", "detail": "cache cleared"}

    # ------------------------------------------------------------------ domains

    @app.get("/api/v1/domains", response_model=list[DomainInfo], tags=["domains"])
    def list_domains(
        include_dns: bool = Query(True, description="Include generated DNS records"),
        client: MailcowClient = Depends(get_client),
    ) -> list[DomainInfo]:
        return collect_domains(client, include_dns=include_dns)

    @app.get(
        "/api/v1/domains/{domain}",
        response_model=DomainInfo,
        tags=["domains"],
    )
    def get_domain(domain: str, client: MailcowClient = Depends(get_client)) -> DomainInfo:
        info = domain_bundle(client, domain)
        if info is None:
            raise HTTPException(status_code=404, detail=f"domain not found: {domain}")
        return info

    @app.get("/api/v1/alias-domains", response_model=list[AliasDomainInfo], tags=["domains"])
    def list_alias_domains(client: MailcowClient = Depends(get_client)) -> list[AliasDomainInfo]:
        return collect_alias_domains(client)

    # ------------------------------------------------------------------ dkim

    @app.get("/api/v1/dkim", response_model=list[DkimKey], tags=["dkim"])
    def list_dkim(client: MailcowClient = Depends(get_client)) -> list[DkimKey]:
        return collect_dkim(client)

    @app.get("/api/v1/dkim/{domain}", response_model=DkimKey, tags=["dkim"])
    def get_dkim(domain: str, client: MailcowClient = Depends(get_client)) -> DkimKey:
        key = client.dkim_for(domain.strip().lower())
        if key is None:
            raise HTTPException(status_code=404, detail=f"no DKIM key for domain: {domain}")
        return key

    # ------------------------------------------------------------------ dns

    @app.get("/api/v1/dns", response_model=dict[str, list[DnsRecord]], tags=["dns"])
    def list_dns(
        include_srv: bool = Query(True, description="Include the SRV record set"),
        client: MailcowClient = Depends(get_client),
    ) -> dict[str, list[DnsRecord]]:
        return collect_dns(client, include_srv=include_srv)

    @app.get("/api/v1/dns/{domain}", response_model=list[DnsRecord], tags=["dns"])
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

    @app.get("/api/v1/mailboxes", response_model=list[MailboxInfo], tags=["mailboxes"])
    def list_mailboxes(
        domain: str | None = Query(None, description="Filter by domain"),
        client: MailcowClient = Depends(get_client),
    ) -> list[MailboxInfo]:
        return collect_mailboxes(client, domain)

    @app.get("/api/v1/aliases", response_model=list[AliasInfo], tags=["aliases"])
    def list_aliases(
        domain: str | None = Query(None, description="Filter by domain"),
        client: MailcowClient = Depends(get_client),
    ) -> list[AliasInfo]:
        return collect_aliases(client, domain)

    # ------------------------------------------------------------------ export

    @app.get("/api/v1/export", response_model=ExportBundle, tags=["export"])
    def export_all(client: MailcowClient = Depends(get_client)) -> ExportBundle:
        return export_bundle(client)

    @app.get(
        "/api/v1/domains/{domain}/export",
        response_model=DomainInfo,
        tags=["export"],
    )
    def export_domain(domain: str, client: MailcowClient = Depends(get_client)) -> DomainInfo:
        return get_domain(domain, client)

    # ------------------------------------------------------------------ replicate

    @app.post("/api/v1/replicate", response_model=ReplicateResponse, tags=["replicate"])
    def replicate(
        request: ReplicateRequest,
        client: MailcowClient = Depends(get_client),
    ) -> ReplicateResponse:
        """Push this node's config to a peer, or receive a bundle to apply locally."""
        # --- send: {"peer": "10.0.0.2"} -> scrape this node, push to the peer
        if request.peer:
            bundle = request.bundle or export_bundle(client)
            verify_tls = (
                settings.peer_verify_tls if request.verify_tls is None else request.verify_tls
            )
            try:
                return send_bundle(
                    bundle,
                    peer=request.peer,
                    verify_tls=verify_tls,
                    timeout=settings.peer_timeout,
                    prune=request.prune,
                    include_mailboxes=request.include_mailboxes,
                    include_domain_admins=request.include_domain_admins,
                )
            except PeerError as exc:
                raise HTTPException(
                    status_code=status.HTTP_502_BAD_GATEWAY, detail=str(exc)
                ) from exc

        # --- receive: {"bundle": {...}} -> apply it to this node's own mailcow
        if request.bundle is None:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="provide 'peer' to push, or 'bundle' to apply locally",
            )

        desired = desired_from_bundle(request.bundle)
        api = local_write_client(client)
        try:
            plan = build_plan(
                api,
                desired,
                target_url=api.base_url,
                source="bundle",
                prune=request.prune,
                include_mailboxes=request.include_mailboxes,
                include_domain_admins=request.include_domain_admins,
            )
            result = apply_plan(api, plan, desired)
        except MailcowApiError as exc:
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail=f"write target unreachable: {exc}",
            ) from exc
        finally:
            api.close()

        return ReplicateResponse(
            mode="receive",
            domains=len(request.bundle.domains),
            mailboxes=len(request.bundle.mailboxes),
            aliases=len(request.bundle.aliases),
            applied=result.applied,
            failed=result.failed,
            errors=result.errors,
        )

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
