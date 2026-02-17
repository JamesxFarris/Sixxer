from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Application settings loaded from environment variables and .env file."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
    )

    fiverr_username: str = Field(description="Fiverr account username")
    fiverr_password: str = Field(description="Fiverr account password")
    anthropic_api_key: str = Field(description="Anthropic API key for Claude access")
    claude_model: str = Field(
        default="claude-sonnet-4-5-20250929",
        description="Claude model identifier to use for AI tasks",
    )
    daily_cost_cap_usd: float = Field(
        default=5.0,
        description="Maximum daily spend on API calls in USD",
    )
    poll_interval_min: int = Field(
        default=3,
        description="Minimum polling interval in minutes",
    )
    poll_interval_max: int = Field(
        default=5,
        description="Maximum polling interval in minutes",
    )
    log_level: str = Field(
        default="INFO",
        description="Logging level (DEBUG, INFO, WARNING, ERROR, CRITICAL)",
    )
    port: int = Field(
        default=8080,
        description="HTTP port for the health check server (set by Railway's $PORT)",
    )
    db_path: str = Field(
        default="data/sixxer.db",
        description="Path to the SQLite database file",
    )
    browser_data_dir: str = Field(
        default="data/browser_data",
        description="Directory for persistent browser session data",
    )
    deliverables_dir: str = Field(
        default="data/deliverables",
        description="Directory for generated deliverable files",
    )

    @property
    def base_dir(self) -> Path:
        """Return the project root directory."""
        return Path(__file__).resolve().parent.parent

    @property
    def abs_db_path(self) -> Path:
        """Return the absolute path to the database file."""
        return self.base_dir / self.db_path

    @property
    def abs_browser_data_dir(self) -> Path:
        """Return the absolute path to the browser data directory."""
        return self.base_dir / self.browser_data_dir

    @property
    def abs_deliverables_dir(self) -> Path:
        """Return the absolute path to the deliverables directory."""
        return self.base_dir / self.deliverables_dir


settings = Settings()
