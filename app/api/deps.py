"""FastAPI dependency injection: authentication."""
from fastapi import Depends, HTTPException, Security, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer, APIKeyHeader

from app.core.config import get_settings
from app.core.security import verify_api_key, verify_cognito_token

settings = get_settings()

_bearer = HTTPBearer(auto_error=False)
_api_key_header = APIKeyHeader(name="X-API-KEY", auto_error=False)


def require_auth(
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer),
    api_key: str | None = Depends(_api_key_header),
) -> dict:
    """
    Accept either:
      - Bearer <CognitoJWT>   (UI / Cognito users)
      - X-API-KEY <token>     (machine-to-machine)
    """
    if api_key and verify_api_key(api_key):
        return {"sub": "api_key_client", "scope": "external"}

    if credentials and credentials.credentials:
        try:
            payload = verify_cognito_token(credentials.credentials)
            return payload
        except HTTPException:
            raise

    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Missing or invalid credentials. Provide Bearer token or X-API-KEY.",
        headers={"WWW-Authenticate": "Bearer"},
    )


def require_api_key(api_key: str | None = Depends(_api_key_header)) -> str:
    """Strict API-key-only guard (for external service endpoints)."""
    if api_key and verify_api_key(api_key):
        return api_key
    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Invalid or missing X-API-KEY header.",
    )
