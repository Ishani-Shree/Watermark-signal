from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = "postgresql+psycopg://user:pass@localhost:5432/watermark"
    jwt_secret: str = "dev-secret-change-me"
    provider: str = "replay"  # 'yfinance' | 'replay'
    env: str = "dev"

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


settings = Settings()
