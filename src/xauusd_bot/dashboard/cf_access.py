"""Cloudflare Access JWT verification for dashboard SSO pass-through.

When the dashboard sits behind Cloudflare Access, each request carries a
signed JWT (the ``Cf-Access-Jwt-Assertion`` header / ``CF_Authorization``
cookie). We verify it against the team's JWKS and the application audience so
an already-authenticated Google / Azure-AD user is logged in without the
local password.

Security: the JWT is **cryptographically verified** — RS256 signature against
the team JWKS, ``aud`` == the application's audience tag, ``iss`` == the team
domain, plus expiry. The header is NEVER trusted unverified, so a request that
reaches the origin bypassing Cloudflare cannot forge an identity.
"""

from __future__ import annotations

import asyncio
import json
import time

import httpx
import jwt
import structlog
from jwt.algorithms import RSAAlgorithm

log = structlog.get_logger(__name__)

_JWKS_TTL_SECONDS = 600.0


class CloudflareAccessVerifier:
    """Verify Cloudflare Access JWTs against a team's JWKS + application AUD."""

    def __init__(self, team_domain: str, aud: str, *, jwks_ttl: float = _JWKS_TTL_SECONDS) -> None:
        domain = team_domain.strip().rstrip("/").removeprefix("https://").removeprefix("http://")
        self._issuer = f"https://{domain}"
        self._certs_url = f"{self._issuer}/cdn-cgi/access/certs"
        self._aud = aud
        self._ttl = jwks_ttl
        self._keys: dict[str, object] = {}  # kid -> public key
        self._fetched_at = 0.0
        self._lock = asyncio.Lock()

    @property
    def certs_url(self) -> str:
        return self._certs_url

    async def _refresh_keys(self, *, force: bool = False) -> None:
        fresh = self._keys and (time.monotonic() - self._fetched_at) < self._ttl
        if fresh and not force:
            return
        async with self._lock:
            fresh = self._keys and (time.monotonic() - self._fetched_at) < self._ttl
            if fresh and not force:
                return
            async with httpx.AsyncClient(timeout=5.0) as client:
                resp = await client.get(self._certs_url)
                resp.raise_for_status()
                jwks = resp.json()
            keys: dict[str, object] = {}
            for jwk in jwks.get("keys", []):
                kid = jwk.get("kid")
                if kid:
                    keys[kid] = RSAAlgorithm.from_jwk(json.dumps(jwk))
            self._keys = keys
            self._fetched_at = time.monotonic()

    async def _key_for(self, kid: str):
        if kid not in self._keys:
            # Unknown kid → Cloudflare may have rotated keys; refresh once.
            await self._refresh_keys(force=True)
        return self._keys.get(kid)

    async def verify(self, token: str) -> str | None:
        """Return the verified user e-mail, or None when the token is invalid."""
        try:
            await self._refresh_keys()
            kid = jwt.get_unverified_header(token).get("kid", "")
            key = await self._key_for(kid)
            if key is None:
                return None
            claims = jwt.decode(
                token,
                key=key,
                algorithms=["RS256"],
                audience=self._aud,
                issuer=self._issuer,
                options={"require": ["exp", "iat", "iss", "aud"]},
            )
            email = claims.get("email") or claims.get("identity")
            return str(email) if email else None
        except Exception as exc:  # noqa: BLE001 - any failure means unauthenticated
            log.debug("cf_access_verify_failed", error=str(exc))
            return None


__all__ = ["CloudflareAccessVerifier"]
