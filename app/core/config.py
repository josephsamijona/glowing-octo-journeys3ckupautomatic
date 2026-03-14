from functools import lru_cache
from typing import Optional

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="allow",
    )

    # --- App ---
    app_env: str = "production"
    secret_key: str = "changeme"

    # --- AWS Cognito ---
    cognito_user_pool_id: str = ""
    cognito_app_client_id: str = ""
    cognito_region: str = "us-east-1"

    # --- External API Key ---
    external_backend_secret_token: str = ""

    # --- Database to backup ---
    db_url: str = ""

    # --- DynamoDB ---
    dynamodbtable: str = "BackupTasks"
    aws_access_key_id: str = ""
    aws_secret_access_key: str = ""
    aws_region: str = "us-east-1"

    # --- Redis / Celery ---
    redis_url: str = "redis://localhost:6379"
    celery_broker_url: str = "redis://localhost:6379"

    # --- Email (Resend) ---
    resend_api_key: str = ""
    email_from: str = "backup@jhbridgetranslation.com"

    # --- S3 ---
    s3_bucket_name: str = "jhbridge-mysql-backups"

    # --- Admin ---
    admin_email: str = Field(default="admin@jhbridgetranslation.com")

    # --- CORS ---
    # Comma-separated list of allowed origins, or "*" for development.
    # Production: "https://backup.jhbridgetranslation.com"
    allowed_origins: str = "*"

    @property
    def cognito_jwks_url(self) -> str:
        return (
            f"https://cognito-idp.{self.cognito_region}.amazonaws.com/"
            f"{self.cognito_user_pool_id}/.well-known/jwks.json"
        )


@lru_cache()
def get_settings() -> Settings:
    return Settings()
