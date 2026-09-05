from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = "postgresql+psycopg://user:pass@localhost:5432/watermark"
    jwt_secret: str = "dev-secret-change-me"
    provider: str = "replay"  # 'yfinance' | 'replay'
    env: str = "dev"

    # Demo controls are gated on their own switch rather than on which
    # provider is configured, so the deployment can run on live data and
    # still replay a scripted day on demand.
    demo_controls: bool = True

    # Comma-separated exact origins. Local Vite ports are included because
    # it picks the next free one when 5173 is taken.
    cors_origins: str = (
        "https://watermark-signal.pages.dev,"
        "http://localhost:5173,http://localhost:5174,http://localhost:5175,"
        "http://127.0.0.1:5173,http://127.0.0.1:5174,http://127.0.0.1:5175"
    )

    @property
    def cors_origin_list(self) -> list[str]:
        return [origin.strip() for origin in self.cors_origins.split(",") if origin.strip()]


INSECURE_JWT_DEFAULT = "dev-secret-change-me"

settings = Settings()

# The development default is published in this repository, so anyone could
# mint a token for any account with it. Refuse to start rather than run
# publicly with a known signing key -- a misconfigured deploy should fail
# loudly, not serve traffic that looks fine and is silently forgeable.
if settings.env == "production" and settings.jwt_secret == INSECURE_JWT_DEFAULT:
    raise RuntimeError(
        "JWT_SECRET is still the development default. Set a real secret "
        "before running in production: python -c \"import secrets; "
        "print(secrets.token_urlsafe(32))\""
    )
