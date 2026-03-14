import hmac
from functools import lru_cache

import httpx
from fastapi import HTTPException, status
from jose import JWTError, jwt

from app.core.config import get_settings

settings = get_settings()


@lru_cache(maxsize=1)
def _fetch_jwks() -> dict:
    """Fetch and cache Cognito JWKS (JSON Web Key Set)."""
    with httpx.Client(timeout=10) as client:
        resp = client.get(settings.cognito_jwks_url)
        resp.raise_for_status()
        return resp.json()


def verify_cognito_token(token: str) -> dict:
    """Validate a Cognito-issued JWT and return the decoded payload."""
    try:
        jwks = _fetch_jwks()
        header = jwt.get_unverified_header(token)

        key = next(
            (k for k in jwks.get("keys", []) if k["kid"] == header.get("kid")),
            None,
        )
        if not key:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Token signing key not found.",
            )

        payload = jwt.decode(
            token,
            key,
            algorithms=["RS256"],
            audience=settings.cognito_app_client_id,
        )
        return payload

    except JWTError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            # Don't reflect internal JWT details back to the caller
            detail="Invalid or expired token.",
        ) from exc


def verify_api_key(api_key: str) -> bool:
    """
    Constant-time comparison — prevents timing-oracle attacks.
    hmac.compare_digest runs in fixed time regardless of how many
    bytes match, so an attacker cannot brute-force the key
    byte-by-byte by measuring response latency.
    """
    secret = settings.external_backend_secret_token
    if not api_key or not secret:
        return False
    return hmac.compare_digest(api_key.encode(), secret.encode())
