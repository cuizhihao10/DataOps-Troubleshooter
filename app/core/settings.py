"""集中式环境配置模型。

所有预算、路径、超时和连接信息都通过 pydantic-settings 进入应用，避免魔法数字散落。
数据库 URL 使用 SecretStr，健康检查和日志只报告连接状态，不输出认证信息。
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from app.retrieval.models import EvidenceBundleBudget, HybridScoringWeights


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
    react_total_timeout_seconds: float = Field(default=60, gt=0, le=600)
    max_graph_hops: int = Field(default=2, ge=1, le=2)
    max_audit_revisions: int = Field(default=1, ge=0, le=1)
    tool_timeout_seconds: float = Field(default=5, gt=0, le=60)
    tool_retry_count: int = Field(default=1, ge=0, le=1)

    embedding_provider: str = "deterministic-hash:v1"
    embedding_dimensions: int = Field(default=128, ge=8, le=4096)
    retrieval_semantic_weight: float = Field(default=0.45, ge=0, le=1)
    retrieval_lexical_weight: float = Field(default=0.10, ge=0, le=1)
    retrieval_path_weight: float = Field(default=0.25, ge=0, le=1)
    retrieval_reliability_weight: float = Field(default=0.10, ge=0, le=1)
    retrieval_freshness_weight: float = Field(default=0.10, ge=0, le=1)
    retrieval_context_max_bytes: int = Field(default=6000, ge=256, le=100_000)
    retrieval_context_max_nodes: int = Field(default=8, ge=1, le=50)
    retrieval_context_max_paths: int = Field(default=4, ge=0, le=20)

    fixture_directory: Path = Path("data/fixtures/scenarios")
    golden_case_file: Path = Path("data/fixtures/golden_cases.json")
    knowledge_seed_file: Path = Path("data/knowledge/cross_chain_graph.json")
    database_url: SecretStr | None = None

    planner_prompt_id: str = "planner-react:v1"
    mcp_contract_id: str = "mcp-tools:v1"
    golden_case_contract_id: str = "golden-case:v1"
    capabilities_contract_id: str = "runtime-capabilities:v1"
    react_loop_contract_id: str = "langgraph-react-loop:v1"
    graphrag_retrieval_contract_id: str = "graphrag-retrieval:v2"
    graphrag_evidence_bundle_contract_id: str = "graphrag-evidence-bundle:v1"

    @model_validator(mode="after")
    def validate_retrieval_configuration(self) -> Settings:
        """在应用启动时校验混合评分和上下文预算，而不是等到首次检索才暴露错误。

        两个 Pydantic 配置模型复用总和与范围契约；Provider 名称和维度由工厂继续校验，从而把
        通用配置一致性与具体 Provider 支持范围分开，任一错误都会阻止半配置实例启动。
        """

        self.hybrid_scoring_weights()
        self.evidence_bundle_budget()
        return self

    def hybrid_scoring_weights(self) -> HybridScoringWeights:
        """把环境变量中的五个独立权重组装为不可变检索评分配置。

        独立字段让 `.env` 可以直接覆盖每一项，返回的 Pydantic 模型则为检索服务和健康检查提供
        类型化快照；若权重和不为一，本方法抛出 ValidationError 而不进行隐式归一化。
        """

        return HybridScoringWeights(
            semantic=self.retrieval_semantic_weight,
            lexical=self.retrieval_lexical_weight,
            path=self.retrieval_path_weight,
            reliability=self.retrieval_reliability_weight,
            freshness=self.retrieval_freshness_weight,
        )

    def evidence_bundle_budget(self) -> EvidenceBundleBudget:
        """把环境中的字节、节点和路径上限组装为不可变 Evidence Bundle 预算。

        三项限制分别防止长文本、过多短节点和过多关系路径挤占 Planner 上下文；返回 Pydantic 模型
        让构建器、健康接口和测试共享同一边界，非法值在 Settings 初始化阶段失败。
        """

        return EvidenceBundleBudget(
            max_bytes=self.retrieval_context_max_bytes,
            max_nodes=self.retrieval_context_max_nodes,
            max_paths=self.retrieval_context_max_paths,
        )


@lru_cache
def get_settings() -> Settings:
    """构造并缓存进程级 Settings，确保所有组件看到同一份已校验配置。

    配置解析可能读取环境文件，缓存可避免每个请求重复 I/O，也防止运行中环境变量变化造成
    同一诊断使用不同预算。测试若需切换环境，应显式调用 `get_settings.cache_clear()` 后重建，
    而不是修改已创建的 Settings 对象。
    """

    return Settings()
