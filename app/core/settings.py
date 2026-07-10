from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_prefix="DATAOPS_",
        extra="ignore",
    )

    app_name: str = "DataOps Troubleshooter"
    app_version: str = "0.1.0"
    environment: str = "development"
    log_level: str = "INFO"
    host: str = "0.0.0.0"
    port: int = Field(default=8000, ge=1, le=65535)

    max_react_steps: int = Field(default=6, ge=1, le=20)
    max_graph_hops: int = Field(default=2, ge=1, le=2)
    max_audit_revisions: int = Field(default=1, ge=0, le=1)
    tool_timeout_seconds: float = Field(default=5, gt=0, le=60)
    tool_retry_count: int = Field(default=1, ge=0, le=1)

    fixture_directory: Path = Path("data/fixtures/scenarios")
    golden_case_file: Path = Path("data/fixtures/golden_cases.json")

    planner_prompt_id: str = "planner-react:v1"
    mcp_contract_id: str = "mcp-tools:v1"
    golden_case_contract_id: str = "golden-case:v1"


@lru_cache
def get_settings() -> Settings:
    return Settings()
