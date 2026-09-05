from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = "postgresql+psycopg://user:pass@localhost:5432/watermark"
    jwt_secret: str = "dev-secret-change-me"
    provider: str = "replay"  # 'yfinance' | 'replay'
    env: str = "dev"


settings = Settings()
