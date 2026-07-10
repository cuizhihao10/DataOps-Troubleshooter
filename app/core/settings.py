"""集中式环境配置模型。

所有预算、路径、超时和连接信息都通过 pydantic-settings 进入应用，避免魔法数字散落。
数据库 URL 使用 SecretStr，健康检查和日志只报告连接状态，不输出认证信息。
"""

from functools import lru_cache
from pathlib import Path

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """集中声明应用配置、运行预算、资产路径和可选数据库连接。

    `pydantic-settings` 从 `DATAOPS_` 环境变量与 `.env` 读取值，并在进程启动时执行范围校验；
    因此业务代码只接收合法预算而无需重复解析字符串。数据库 URL 使用 SecretStr，避免对象
    被日志或异常直接格式化时泄露凭据；额外环境变量被忽略以兼容共享部署环境。
    """

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
    knowledge_seed_file: Path = Path("data/knowledge/cross_chain_graph.json")
    database_url: SecretStr | None = None

    planner_prompt_id: str = "planner-react:v1"
    mcp_contract_id: str = "mcp-tools:v1"
    golden_case_contract_id: str = "golden-case:v1"


@lru_cache
def get_settings() -> Settings:
    """构造并缓存进程级 Settings，确保所有组件看到同一份已校验配置。

    配置解析可能读取环境文件，缓存可避免每个请求重复 I/O，也防止运行中环境变量变化造成
    同一诊断使用不同预算。测试若需切换环境，应显式调用 `get_settings.cache_clear()` 后重建，
    而不是修改已创建的 Settings 对象。
    """

    return Settings()
