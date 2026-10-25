"""Thin wrapper around the Docker CLI for exec-ing into mailcow containers.

mailcow does not set ``container_name``, so containers are named
``<project>-<service>-1``. Rather than hard-coding that, we resolve the running
container id by its compose labels:

    docker ps --filter label=com.docker.compose.service=redis-mailcow \\
              --filter label=com.docker.compose.project=mailcowdockerized
"""

from __future__ import annotations

import subprocess
import time


class DockerError(RuntimeError):
    """Raised when the Docker CLI is missing, fails, or a container is absent."""


class DockerClient:
    def __init__(
        self,
        binary: str = "docker",
        project: str | None = None,
        timeout: float = 30.0,
        cache_ttl: float = 30.0,
    ) -> None:
        self.binary = binary
        self.project = project
        self.timeout = timeout
        self.cache_ttl = cache_ttl
        self._containers: dict[str, tuple[str, float]] = {}

    def _run(
        self, argv: list[str], input: str | None = None
    ) -> subprocess.CompletedProcess[str]:
        try:
            return subprocess.run(
                argv,
                input=input,
                capture_output=True,
                text=True,
                timeout=self.timeout,
            )
        except FileNotFoundError as exc:
            raise DockerError(f"docker binary not found: {argv[0]!r}") from exc
        except subprocess.TimeoutExpired as exc:
            raise DockerError(f"command timed out after {self.timeout}s: {' '.join(argv)}") from exc

    def container_id(self, service: str, refresh: bool = False) -> str:
        now = time.monotonic()
        cached = self._containers.get(service)
        if cached and not refresh and now < cached[1]:
            return cached[0]

        argv = [self.binary, "ps", "--filter", f"label=com.docker.compose.service={service}"]
        if self.project:
            argv += ["--filter", f"label=com.docker.compose.project={self.project}"]
        argv += ["--format", "{{.ID}}"]

        result = self._run(argv)
        if result.returncode != 0:
            raise DockerError(
                f"docker ps failed for service {service!r}: "
                f"{result.stderr.strip() or result.stdout.strip()}"
            )

        ids = [line.strip() for line in result.stdout.splitlines() if line.strip()]
        if not ids:
            raise DockerError(
                f"no running container found for service {service!r} "
                f"(project={self.project!r}). Is mailcow up?"
            )

        container_id = ids[0]
        self._containers[service] = (container_id, now + self.cache_ttl)
        return container_id

    def exec(
        self,
        service: str,
        argv: list[str],
        env: dict[str, str] | None = None,
        input: str | None = None,
    ) -> str:
        """Run ``argv`` inside the container for ``service`` and return stdout."""

        def build(container_id: str) -> list[str]:
            cmd = [self.binary, "exec", "-i"]
            for key, value in (env or {}).items():
                cmd += ["-e", f"{key}={value}"]
            cmd.append(container_id)
            cmd += list(argv)
            return cmd

        container_id = self.container_id(service)
        result = self._run(build(container_id), input=input)

        # The container may have been recreated since we cached its id.
        if result.returncode != 0:
            container_id = self.container_id(service, refresh=True)
            result = self._run(build(container_id), input=input)

        if result.returncode != 0:
            detail = result.stderr.strip() or result.stdout.strip()
            raise DockerError(f"exec failed in {service}: {detail}")
        return result.stdout
