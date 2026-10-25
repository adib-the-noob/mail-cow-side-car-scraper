"""Optional bearer-token auth for the API surface."""

from __future__ import annotations

from fastapi import Header, HTTPException, status


def build_auth_dependency(token: str | None):
    """Return a FastAPI dependency enforcing ``Authorization: Bearer <token>``.

    When ``token`` is falsy the dependency is a no-op, so the API is open (only
    sensible when bound to localhost).
    """

    async def require_token(authorization: str | None = Header(default=None)) -> None:
        if not token:
            return
        if authorization != f"Bearer {token}":
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="invalid or missing bearer token",
                headers={"WWW-Authenticate": "Bearer"},
            )

    return require_token
