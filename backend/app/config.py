from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    database_url: str = "postgresql+psycopg://user:pass@localhost:5432/watermark"
    jwt_secret: str = "dev-secret-change-me"
    provider: str = "replay"  # 'yfinance' | 'replay'
    env: str = "dev"

    class Config:
        env_file = ".env"


settings = Settings()
