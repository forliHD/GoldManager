"""Cloudflare Access JWT verification — signature, aud, issuer, expiry, email."""

from __future__ import annotations

import json
import time
from datetime import UTC, datetime, timedelta

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from jwt.algorithms import RSAAlgorithm

from xauusd_bot.dashboard.cf_access import CloudflareAccessVerifier

TEAM = "it-reiser.cloudflareaccess.com"
ISS = f"https://{TEAM}"
AUD = "test-aud-tag"
KID = "key-1"


@pytest.fixture(scope="module")
def keypair():
    priv = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pub_jwk = json.loads(RSAAlgorithm.to_jwk(priv.public_key()))
    pub_jwk["kid"] = KID
    return priv, pub_jwk


def _verifier(pub_jwk) -> CloudflareAccessVerifier:
    v = CloudflareAccessVerifier(TEAM, AUD)
    # Inject the public key so verify() never hits the network.
    v._keys = {KID: RSAAlgorithm.from_jwk(json.dumps(pub_jwk))}
    v._fetched_at = time.monotonic()
    return v


def _token(priv, *, aud=AUD, iss=ISS, email="user@it-reiser.de", exp_delta=300, kid=KID):
    now = datetime.now(tz=UTC)
    claims = {"aud": aud, "iss": iss, "iat": now, "exp": now + timedelta(seconds=exp_delta)}
    if email is not None:
        claims["email"] = email
    return jwt.encode(claims, priv, algorithm="RS256", headers={"kid": kid})


@pytest.mark.asyncio
async def test_valid_token_returns_email(keypair):
    priv, pub = keypair
    v = _verifier(pub)
    assert await v.verify(_token(priv)) == "user@it-reiser.de"


@pytest.mark.asyncio
async def test_wrong_aud_rejected(keypair):
    priv, pub = keypair
    v = _verifier(pub)
    assert await v.verify(_token(priv, aud="some-other-app")) is None


@pytest.mark.asyncio
async def test_wrong_issuer_rejected(keypair):
    priv, pub = keypair
    v = _verifier(pub)
    assert await v.verify(_token(priv, iss="https://evil.cloudflareaccess.com")) is None


@pytest.mark.asyncio
async def test_expired_token_rejected(keypair):
    priv, pub = keypair
    v = _verifier(pub)
    assert await v.verify(_token(priv, exp_delta=-10)) is None


@pytest.mark.asyncio
async def test_bad_signature_rejected(keypair):
    _priv, pub = keypair
    other = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    v = _verifier(pub)  # trusts KID -> the fixture's public key
    # Signed with a different key but same kid -> signature check must fail.
    assert await v.verify(_token(other)) is None


@pytest.mark.asyncio
async def test_missing_email_returns_none(keypair):
    priv, pub = keypair
    v = _verifier(pub)
    assert await v.verify(_token(priv, email=None)) is None
