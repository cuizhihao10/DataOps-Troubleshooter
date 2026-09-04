"""集中式环境配置模型。

所有预算、路径、超时和连接信息都通过 pydantic-settings 进入应用，避免魔法数字散落。
数据库 URL 使用 SecretStr，健康检查和日志只报告连接状态，不输出认证信息。
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import AnyHttpUrl, Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from app.agents.retrying import TransientRetryPolicy
from app.domain.planner import MAX_PARALLEL_TOOL_ACTIONS
from app.retrieval.documents import DocumentScoringWeights
from app.retrieval.embeddings import (
    BGE_M3_DIMENSIONS,
    BGE_M3_PROVIDER_ID,
    DETERMINISTIC_HASH_PROVIDER_ID,
)
from app.retrieval.models import EvidenceBundleBudget, HybridScoringWeights
from app.retrieval.reranker import DISABLED_RERANKER_PROVIDER_ID


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

    max_react_steps: int = Field(default=10, ge=1, le=20)
    # 并行度与步数预算是两个独立旋钮：并行只把"状态 + 日志 + 拓扑"这类互不依赖的取证压进同一段
    # 等待时间，一批 N 个 Action 仍然消耗 N 个步数。因此调大这个值只会降低 P95 延迟，不会让模型
    # 获得更多取证机会，也不需要同步调大 max_react_steps。
    max_parallel_tool_actions: int = Field(default=3, ge=1, le=MAX_PARALLEL_TOOL_ACTIONS)
    # 步数预算必须同时满足两个下界，两者都是被真实模型撞出来的，不是估算：
    # (1) 严格大于 Golden 集里最长的必需工具集（跨组件案例 required_tools 最多 6 个），否则系统被
    #     设计成必然拿不到满覆盖——真实模型总要花一到两步试探，零余量下每次试探都直接换掉一个必需
    #     取证。首次真实模型评测就撞上了这一点：跨组件案例执行满 6 步后以 react_budget_exhausted
    #     结束，却漏掉 bds.get_table_info，于是预算从 6 提到 8。
    # (2) 还要为"收口回合"留出余量。循环在 react_step >= max_steps 时**先停机再判断**，所以把预算
    #     用到刚好等于上限的 Planner 永远拿不到最后那次决策机会：证据其实齐了，却只能以
    #     react_budget_exhausted 结束，报告基于"调查未完成"起草，Auditor 判 report_incomplete，
    #     一次返工预算用尽后转 safe_degraded。8 步下这条恰好会命中——实测一次 3+3+2 的批次序列刚好
    #     填满 8 步，而最后那两个 Action 取的正是 Golden 要求的必需证据，因此"让 Prompt 更早收口"
    #     只会用覆盖率换一个好看的 stop_reason，方向是反的。10 步在同一条轨迹上留出一个整批余量。
    #     结构性的解法是给"只允许 finish 的最后一回合"单列保留额度，那会改动
    #     langgraph-react-loop 的循环语义，属于另一个切片；这里先按可配置项校准。
    # 墙钟预算随后从 60s 放宽到 240s：实测 Planner 单次 8–18s，10 步最坏要五次决策加上工具与检索
    # 时间（实测一次 3 批次的真实 run 端到端 96.7s），仍按 60s 会把"预算够但时间不够"伪装成正常
    # 终止；240s 还必须容得下至少一次瞬时重试的最坏开销（见下面 chat_transient_retry_* 与
    # model_transient_retry 校验），否则一个本可恢复的 429 会被预算截断成 total_timeout，等于加了
    # 重试又不让它生效。
    react_total_timeout_seconds: float = Field(default=240, gt=0, le=600)
    max_graph_hops: int = Field(default=2, ge=1, le=2)
    max_audit_revisions: int = Field(default=1, ge=0, le=1)
    tool_timeout_seconds: float = Field(default=5, gt=0, le=60)
    tool_retry_count: int = Field(default=1, ge=0, le=1)

    # MCP 传输选型。生产形态是 Streamable HTTP：网关与 Agent 分属不同部署单元，client↔server 这
    # 一跳必须过网络。默认值仍留 stdio，因为 `tests/conftest.py` 会清空所有 DATAOPS_*，若默认是
    # HTTP，任何应用启动（健康检查测试、/demo、--skip-postgres 离线评测）都要先有一个可达网关。
    # 默认值只代表"零配置能跑通"，不代表推荐形态——生产形态由 compose 显式声明。
    mcp_transport: Literal["stdio", "streamable-http"] = "stdio"
    mcp_http_url: AnyHttpUrl = AnyHttpUrl("http://127.0.0.1:8900/mcp")
    mcp_http_host: str = "0.0.0.0"
    mcp_http_port: int = Field(default=8900, ge=1, le=65535)
    mcp_auth_token: SecretStr | None = None
    # 网关限流与 API 限流不能共用一份配额。API 的 120/60s 是按"人 + 前端轮询"的量级定的；网关侧
    # 一次 call_tool 在无状态模式下是三个 POST（initialize、initialized 通知、tools/call），而且全部
    # 来自同一个 api 容器 IP，所以按来源计的窗口实际是网关的全局闸门。默认 600/60s 能容下单 run
    # 满预算取证（10 步 × 3 POST）与若干并发 run，同时仍然挡住"把网关当通用查询接口刷"。
    mcp_rate_limit_requests: int = Field(default=600, ge=1, le=100_000)
    mcp_rate_limit_window_seconds: float = Field(default=60, gt=0, le=3600)

    # 鉴权默认关闭，让本地演示与测试无需令牌即可跑通；但一旦设置了令牌就必须同时把模式切到
    # bearer，否则实例会拒绝启动——"配了令牌却没启用"比"完全没配"更危险，因为部署者以为接口
    # 已经受保护。限流对已鉴权和未鉴权请求同样生效，配额按来源 IP 计。
    api_auth_mode: Literal["disabled", "bearer"] = "disabled"
    api_auth_token: SecretStr | None = None
    # 默认 120/60s 高于 Demo 前端退避轮询的峰值（每轮两次请求、最短 600ms 间隔），因此正常演示
    # 不会被自己的轮询限流，而脚本化的暴力调用会立刻撞上配额。
    api_rate_limit_requests: int = Field(default=120, ge=1, le=100_000)
    api_rate_limit_window_seconds: float = Field(default=60, gt=0, le=3600)

    chat_provider: Literal["disabled", "openai-compatible"] = "disabled"
    chat_model: str = Field(default="gpt-5.6", min_length=1, max_length=200)
    chat_base_url: AnyHttpUrl = AnyHttpUrl("https://api.openai.com/v1")
    chat_api_key: SecretStr | None = None
    chat_timeout_seconds: float = Field(default=30, gt=0, le=300)
    # Auditor 单独一个超时旋钮，而不是复用 chat_timeout_seconds：首次真实模型评测里两个角色的延迟
    # 分布差一个量级（Planner 实测 8–15s，Auditor 实测 22–30s，因为它要读完整草稿并逐条判定引用，
    # 输出也接近 1100 token）。共用 30s 的后果被实测抓到：四次 Auditor 调用有三次超时，而超时按
    # AuditorAgentError 直接降级且不消耗返工预算，于是三个案例的 accepted_report_rate 全是 0——
    # 审计路径整条失效。Planner 必须保持紧超时（它跑在 react_total_timeout_seconds 预算内，一次
    # 挂死就吃掉整轮），Auditor 不在该预算内，所以放宽它不会拖长 ReAct 循环。
    auditor_timeout_seconds: float = Field(default=90, gt=0, le=300)
    planner_schema_repair_count: int = Field(default=1, ge=0, le=1)
    auditor_schema_repair_count: int = Field(default=1, ge=0, le=1)
    # 瞬时重试与 Schema 修复是两套预算，故意分开：修复预算处理"模型输出格式不对"，重试预算处理
    # "请求根本没打到模型"。第二次真实模型评测实测到必须有它——新端点在约 92s 内成功 6 次调用后
    # 连续 4 次返回 HTTP 错误（每次 250–630ms 即被驳回），两个案例因此以 planner_provider_error
    # 终结，其中一个连第一个工具都没执行，必要 Action 覆盖率直接归零。默认只重试一次：实测那种
    # 配额窗口打满的形态重试更多次也救不回来，只会把单次决策的最坏耗时推出墙钟预算。
    chat_transient_retry_attempts: int = Field(default=2, ge=1, le=3)
    chat_transient_retry_backoff_seconds: float = Field(default=1.0, gt=0, le=30)
    chat_transient_retry_max_backoff_seconds: float = Field(default=8.0, gt=0, le=60)

    # 离线默认仍是确定性基线，使无凭据环境可以真实跑通 pgvector；生产部署改成 bge-m3:v1 并配
    # 套 1024 维，两者通过 provider_id 隔离，绝不会在同一次 cosine 排序里混算。
    embedding_provider: str = "deterministic-hash:v1"
    embedding_dimensions: int = Field(default=128, ge=8, le=4096)
    embedding_model: str = Field(default="BAAI/bge-m3", min_length=1, max_length=200)
    embedding_base_url: AnyHttpUrl = AnyHttpUrl("https://api.siliconflow.cn/v1")
    embedding_api_key: SecretStr | None = None
    embedding_timeout_seconds: float = Field(default=30, gt=0, le=300)
    embedding_batch_size: int = Field(default=32, ge=1, le=256)

    rerank_provider: str = "disabled"
    rerank_model: str = Field(default="BAAI/bge-reranker-v2-m3", min_length=1, max_length=200)
    rerank_base_url: AnyHttpUrl = AnyHttpUrl("https://api.siliconflow.cn/v1")
    rerank_api_key: SecretStr | None = None
    rerank_timeout_seconds: float = Field(default=20, gt=0, le=300)
    rerank_candidate_multiplier: int = Field(default=3, ge=1, le=8)
    rerank_blend_weight: float = Field(default=0.4, ge=0, le=1)

    retrieval_semantic_weight: float = Field(default=0.45, ge=0, le=1)
    retrieval_lexical_weight: float = Field(default=0.10, ge=0, le=1)
    retrieval_path_weight: float = Field(default=0.25, ge=0, le=1)
    retrieval_reliability_weight: float = Field(default=0.10, ge=0, le=1)
    retrieval_freshness_weight: float = Field(default=0.10, ge=0, le=1)
    retrieval_context_max_bytes: int = Field(default=6000, ge=256, le=100_000)
    retrieval_context_max_nodes: int = Field(default=8, ge=1, le=50)
    retrieval_context_max_paths: int = Field(default=4, ge=0, le=20)
    retrieval_context_max_documents: int = Field(default=3, ge=0, le=20)
    diagnosis_retrieval_seed_limit: int = Field(default=5, ge=1, le=20)
    # 文档通道的三因子权重必须和为一；authority 直接取文档人工声明的 reliability，因此把它调高
    # 等于宣布"越权威的文档越优先"，这是一个应当显式配置而不是隐藏在代码里的产品判断。
    document_semantic_weight: float = Field(default=0.60, ge=0, le=1)
    document_lexical_weight: float = Field(default=0.25, ge=0, le=1)
    document_authority_weight: float = Field(default=0.15, ge=0, le=1)
    document_retrieval_chunk_limit: int = Field(default=4, ge=1, le=20)
    memory_dedup_similarity_threshold: float = Field(default=0.92, ge=0, le=1)
    case_graph_similarity_threshold: float = Field(default=0.75, ge=0, le=1)
    memory_search_limit: int = Field(default=5, ge=1, le=20)
    memory_query_max_chars: int = Field(default=4000, ge=256, le=20_000)

    # Worker 的短轮询、租约和心跳都集中配置，避免队列恢复边界散落在 API/数据库代码中。
    diagnosis_worker_poll_seconds: float = Field(default=0.25, gt=0, le=10)
    diagnosis_worker_lease_seconds: float = Field(default=180, gt=1, le=3600)
    diagnosis_worker_heartbeat_seconds: float = Field(default=30, gt=0, le=600)
    diagnosis_worker_max_attempts: int = Field(default=2, ge=1, le=5)

    # SSE 推流是"读侧"配置，与 Worker 的执行预算完全独立：轮询间隔决定事件出现在浏览器上的延迟
    # 下限，心跳周期负责让反向代理相信连接仍然活着，最长存活时间保证一条被遗忘的标签页不会永久
    # 占用连接。推流只读已落库数据，因此把这三个值调错最坏的后果是"演示时要退回轮询"，不会影响
    # 任何 run 的结论。
    run_stream_poll_seconds: float = Field(default=0.5, gt=0, le=10)
    run_stream_keepalive_seconds: float = Field(default=15, gt=0, le=120)
    run_stream_max_seconds: float = Field(default=300, gt=1, le=3600)

    fixture_directory: Path = Path("data/fixtures/scenarios")
    golden_case_file: Path = Path("data/fixtures/golden_cases.json")
    knowledge_seed_file: Path = Path("data/knowledge/cross_chain_graph.json")
    document_manifest_file: Path = Path("data/knowledge/documents/manifest.json")
    database_url: SecretStr | None = None

    planner_prompt_id: str = "planner-react:v8"
    planner_provider_contract_id: str = "openai-compatible-planner:v1"
    auditor_prompt_id: str = "auditor-report:v2"
    auditor_provider_contract_id: str = "openai-compatible-auditor:v1"
    # 重试是包装层而不是 Provider 内部行为，因此单列一个契约 ID：两个 openai-compatible-*:v1
    # 仍然如实描述"一次 complete 只发一次网络请求"，遥测里每次尝试也仍是独立一条记录。
    model_transient_retry_contract_id: str = "model-transient-retry:v1"
    mcp_contract_id: str = "mcp-tools:v1"
    # 传输与工具契约分开计版：九个工具名和 `McpToolResponse` 一个字未改，所以 mcp-tools:v1 不动，
    # 新增的这一个只描述"client↔server 那一跳怎么连"。理由与上面的 model_transient_retry 相同。
    mcp_transport_contract_id: str = "mcp-transport:v1"
    golden_case_contract_id: str = "golden-case:v10"
    capabilities_contract_id: str = "runtime-capabilities:v1"
    react_loop_contract_id: str = "langgraph-react-loop:v3"
    audited_report_workflow_contract_id: str = "audited-report-workflow:v2"
    diagnosis_workflow_contract_id: str = "audited-diagnosis-workflow:v2"
    diagnosis_api_contract_id: str = "diagnosis-resources:v4"
    session_checkpoint_contract_id: str = "session-checkpoint:v1"
    case_memory_contract_id: str = "case-memory:v2"
    graphrag_retrieval_contract_id: str = "graphrag-retrieval:v3"
    graphrag_evidence_bundle_contract_id: str = "graphrag-evidence-bundle:v3"
    document_retrieval_contract_id: str = "document-retrieval:v1"
    run_trace_contract_id: str = "run-trace:v1"
    api_auth_contract_id: str = "api-auth:v1"
    run_stream_contract_id: str = "run-stream:v1"

    @model_validator(mode="after")
    def validate_runtime_configuration(self) -> Settings:
        """在启动时联合校验检索预算和可选 Planner Provider 的安全配置。

        检索模型复用权重/预算契约；案例图阈值不能高于 canonical 去重阈值；Planner/Auditor 共享
        Chat 端点，禁止 URL 凭据，启用模型时强制 SecretStr key。任一错误都会阻止半配置实例
        启动，而不是延迟到首次模型请求。
        """

        self.hybrid_scoring_weights()
        self.evidence_bundle_budget()
        self.document_scoring_weights()
        # 图关系应覆盖“相似但未达到合并条件”的区间；若阈值更高，配置语义与产品说明相反。
        if self.case_graph_similarity_threshold > self.memory_dedup_similarity_threshold:
            raise ValueError(
                "case_graph_similarity_threshold must not exceed memory dedup threshold"
            )
        # 模型端点不得在 URL 中携带用户名/密码；凭据只能进入 SecretStr chat_api_key。
        if self.chat_base_url.username or self.chat_base_url.password:
            raise ValueError("chat_base_url must not include user information")
        if self.chat_provider != "disabled" and self.chat_api_key is None:
            raise ValueError("chat_api_key is required when chat_provider is enabled")
        # 并行上限超过总步数预算时，控制器每轮都要把 Prompt 里的批次上限压回剩余步数，配置写的值
        # 就永远不会生效；宁可启动失败也不要留下"看起来配了 3 实际只能 1"的误导性配置。
        if self.max_parallel_tool_actions > self.max_react_steps:
            raise ValueError("max_parallel_tool_actions must not exceed max_react_steps")
        if self.diagnosis_worker_heartbeat_seconds >= self.diagnosis_worker_lease_seconds / 2:
            raise ValueError(
                "diagnosis_worker_heartbeat_seconds must be less than half the worker lease"
            )
        self._validate_run_stream()
        self._validate_retrieval_providers()
        self._validate_api_security()
        self._validate_mcp_transport()
        self._validate_transient_retry()
        return self

    def transient_retry_policy(self) -> TransientRetryPolicy:
        """把三个重试旋钮投影成 Provider 包装层使用的策略对象。

        与 `document_scoring_weights` 同样的模式：配置只保存标量，策略语义集中在一个受校验的值
        对象里，避免"退避倍数"这类判断散落在工厂和测试两处。倍数固定为 2.0 而不开放配置——真实
        故障窗口按分钟计，能调的是起始退避和上限，再多一个旋钮只会让最坏耗时更难算。
        """

        return TransientRetryPolicy(
            max_attempts=self.chat_transient_retry_attempts,
            initial_backoff_seconds=self.chat_transient_retry_backoff_seconds,
            max_backoff_seconds=self.chat_transient_retry_max_backoff_seconds,
        )

    def _validate_transient_retry(self) -> None:
        """确认 ReAct 墙钟预算真的容得下一次瞬时重试的最坏开销。

        只加重试而不留预算是自欺欺人：Planner 跑在 `react_total_timeout_seconds` 内，若预算连
        "一次跑满超时的首次尝试 + 全部重试与退避"都装不下，一个本可恢复的 429 会被外层
        `asyncio.timeout` 截断成 total_timeout，指标上看不出重试曾经生效。这里用 Planner 超时而不是
        Auditor 超时，因为 Auditor 不在这个预算内。
        """

        policy = self.transient_retry_policy()
        worst_case = self.chat_timeout_seconds + policy.worst_case_added_seconds(
            self.chat_timeout_seconds
        )
        if self.react_total_timeout_seconds < worst_case:
            raise ValueError(
                "react_total_timeout_seconds must cover one planner call plus its transient retries"
            )

    def _validate_run_stream(self) -> None:
        """校验推流的轮询/心跳/寿命三者有序，且单连接能覆盖一次完整 ReAct 预算。

        轮询必须快于心跳、心跳必须短于寿命，否则这条连接根本等不到第一次心跳，代理仍会按空闲
        超时掐断。更重要的是寿命必须超过 ReAct 总超时：否则一个完全正常、只是跑满预算的 run 会在
        结束前被推流强行截断，演示者看到的是"流断了"而不是"诊断完成"，只能退回轮询才能看到结论。
        """

        if self.run_stream_poll_seconds >= self.run_stream_keepalive_seconds:
            raise ValueError("run_stream_poll_seconds must be shorter than the keepalive period")
        if self.run_stream_keepalive_seconds >= self.run_stream_max_seconds:
            raise ValueError("run_stream_keepalive_seconds must be shorter than the max lifetime")
        if self.run_stream_max_seconds <= self.react_total_timeout_seconds:
            raise ValueError("run_stream_max_seconds must exceed react_total_timeout_seconds")

    def _validate_api_security(self) -> None:
        """校验鉴权模式与令牌的组合，拒绝"看起来受保护、实际裸奔"的半配置实例。

        两个方向都要拦：bearer 缺令牌会让每个请求都 401、等于服务不可用；disabled 却配了令牌则会
        让部署者误以为接口已受保护而把端口暴露出去。令牌强度、字符集和限流器参数由
        `ApiSecurityGuard` 在 lifespan 阶段再次校验，那里持有唯一的策略常量，避免最小长度被写成
        两份而漂移。
        """

        if self.api_auth_mode == "bearer" and self.api_auth_token is None:
            raise ValueError("api_auth_token is required when api_auth_mode is bearer")
        if self.api_auth_mode == "disabled" and self.api_auth_token is not None:
            raise ValueError("api_auth_token must be unset when api_auth_mode is disabled")

    def _validate_mcp_transport(self) -> None:
        """校验 MCP 传输与令牌的组合，并禁止网关地址内嵌凭据。

        `streamable-http` 是网络暴露的工具端点：九个工具虽然只读，暴露出去的是整条链路的排障证据，
        因此缺令牌直接拒绝启动（fail-closed），而不是先跑起来再指望没人扫到它。反方向同样拦：
        stdio 却配了令牌说明部署者以为端点受保护，而 stdio 根本没有可鉴权的网络面。URL 不得携带
        userinfo，否则凭据会随异常文本和 trace 一起外泄——与 chat_base_url 同一条规则。
        """

        if self.mcp_http_url.username or self.mcp_http_url.password:
            raise ValueError("mcp_http_url must not include user information")
        if self.mcp_transport == "streamable-http" and self.mcp_auth_token is None:
            raise ValueError("mcp_auth_token is required when mcp_transport is streamable-http")
        if self.mcp_transport == "stdio" and self.mcp_auth_token is not None:
            raise ValueError("mcp_auth_token must be unset when mcp_transport is stdio")

    def _validate_retrieval_providers(self) -> None:
        """校验 embedding / rerank Provider 与其凭据、维度和端点的一致性。

        远程 Provider 缺 key 会让检索在首次查询才失败并返回空结果，那时部署者已经以为语义检索
        生效；因此启动阶段就要求 SecretStr。维度必须与模型固定输出一致，否则整库向量会写成另一个
        空间；base_url 同样禁止内嵌凭据，避免 URL 出现在异常与 trace 里。
        """

        remote_embedding = self.embedding_provider != DETERMINISTIC_HASH_PROVIDER_ID
        if self.embedding_base_url.username or self.embedding_base_url.password:
            raise ValueError("embedding_base_url must not include user information")
        if self.rerank_base_url.username or self.rerank_base_url.password:
            raise ValueError("rerank_base_url must not include user information")
        if remote_embedding and self.embedding_api_key is None:
            raise ValueError("embedding_api_key is required for remote embedding providers")
        # bge-m3 固定 1024 维；配置漂移只有在此处硬校验才能避免整库写入错误维度后再回滚。
        if self.embedding_provider == BGE_M3_PROVIDER_ID:
            if self.embedding_dimensions != BGE_M3_DIMENSIONS:
                raise ValueError(f"{BGE_M3_PROVIDER_ID} requires {BGE_M3_DIMENSIONS} dimensions")
        if self.rerank_provider != DISABLED_RERANKER_PROVIDER_ID and self.rerank_api_key is None:
            raise ValueError("rerank_api_key is required when rerank_provider is enabled")

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
        """把环境中的字节、节点、路径和文档切片上限组装为不可变 Evidence Bundle 预算。

        四项限制分别防止长文本、过多短节点、过多关系路径和过多文档片段挤占 Planner 上下文；返回
        Pydantic 模型让构建器、健康接口和测试共享同一边界，非法值在 Settings 初始化阶段失败。
        """

        return EvidenceBundleBudget(
            max_bytes=self.retrieval_context_max_bytes,
            max_nodes=self.retrieval_context_max_nodes,
            max_paths=self.retrieval_context_max_paths,
            max_documents=self.retrieval_context_max_documents,
        )

    def document_scoring_weights(self) -> DocumentScoringWeights:
        """把环境中的三个文档权重组装为不可变文档检索评分配置。

        文档通道不使用 path/freshness：切片之间没有关系边，静态文档也没有可信的时效性，硬凑五因子
        只会让公式看起来复杂而不更准确。权重和不为一时抛出 ValidationError，而不做隐式归一化。
        """

        return DocumentScoringWeights(
            semantic=self.document_semantic_weight,
            lexical=self.document_lexical_weight,
            authority=self.document_authority_weight,
        )


@lru_cache
def get_settings() -> Settings:
    """构造并缓存进程级 Settings，确保所有组件看到同一份已校验配置。

    配置解析可能读取环境文件，缓存可避免每个请求重复 I/O，也防止运行中环境变量变化造成
    同一诊断使用不同预算。测试若需切换环境，应显式调用 `get_settings.cache_clear()` 后重建，
    而不是修改已创建的 Settings 对象。
    """

    return Settings()
