# 参考开发 Skill 采纳说明

本文件记录对用户提供的《DataOps Troubleshooter 开发实现 Skill》全部内容的处理结果。它不是新的产品范围；产品基线仍以 `docs/product-design.md` 为准。

| 参考内容 | 处理 | 本项目落地方式 |
|---|---|---|
| 7 天开发周期 | 调整 | 不设固定天数，使用 M0–M4 可验证里程碑。 |
| Python / LangGraph / FastAPI / PostgreSQL / pgvector 依赖 | 采用并更新 | 使用 `pyproject.toml` 管理当前兼容版本；异步数据库优先 `asyncpg`，配置使用 `pydantic-settings`。 |
| Agent、工具、工作流、RAG、记忆、API 分层 | 采用 | 收敛到统一 `app/` 包，并保留独立 `mcp_server/`、`data/` 和 `tests/`。 |
| Agent Skill Library | 采用并改名 | 运行时放入 `app/capabilities/`，固定五类领域能力；避免与 `.agents/skills/` Codex Skill 混淆。 |
| 9 个 MCP 工具 | 完整采用 | 使用 3 个 LTS、3 个 BDS、3 个 FlashSync 只读工具，并为每个工具提供独立测试和异常 Fixture。 |
| 每个工具 3–5 套 Mock | 采用 | 由 `scenario_id` 提供正常、异常、空结果、超时和权限拒绝等可复现场景。 |
| 工具基类与统一返回结构 | 采用并强化 | 使用 MCP 协议、Pydantic 输入 Schema 和统一 `Evidence / ToolEvent / error` 契约。 |
| 向量检索 + 关键词检索 | 采用 | 使用 pgvector 与 PostgreSQL 全文检索合并种子节点，再做去重和混合评分。 |
| 轻量 GraphRAG | 采用 | PostgreSQL 节点/边表 + 1–2 跳关系扩展，不引入 Neo4j。 |
| GraphRAG 实体抽取 Prompt | 采用并加审计 | 只用于离线生成待审核候选，要求 `source_span`、Schema 校验、去重和人工/规则复核。 |
| 社区摘要生成 | 暂缓 | 首版图规模较小，直接返回可追溯路径；只有路径规模影响上下文时再评估摘要。 |
| 核心运行时 Skill | 完整采用并补强 | 单组件诊断、跨组件链路溯源、历史案例匹配、风险评估、结构化报告，作为五个 capabilities 配置。 |
| 动态 Skill 加载 | 收敛 | 使用固定 registry 按意图选择组合，不建设通用插件生态。 |
| LangGraph 多 Agent | 采用 | 固定 Planner ReAct 与 Auditor 两个 LLM Agent，其余节点确定性执行。 |
| ReAct Thought / Action / Observation | 采用并改写 | 保留行为闭环；Reason 隐藏，Action 结构化，Observation 来自真实 MCP；不记录原始思维链。 |
| 工具最大 5 步 | 调整 | 默认最多 6 步，可配置，并增加同参去重、总超时和停止原因。 |
| 工具失败重试 1 次 | 采用并加限制 | 仅瞬时错误可重试；仍失败时只给低置信度知识参考，不冒充实时结论。 |
| 短期记忆与滚动摘要 | 采用 | 会话 checkpoint 保存上下文；达到预算时生成不含思维链的结构化摘要。 |
| 长期记忆自动写入 | 调整 | Auditor 通过后先生成 `pending` 候选，由用户确认或测试配置明确确认，再去重写入。 |
| `/api/troubleshoot`、`/api/chat` | 演进 | 采用会话、消息、运行状态、事件和记忆确认的资源化 `/api/v1` 接口。 |
| 三类核心测试 | 扩展 | 建立 28 个 Golden Cases，覆盖单组件、跨组件、模糊输入、工具异常和记忆召回。 |
| README、架构图、简历包装 | 采用 | 只有实际实现和评测结果可以写入宣传与简历。 |

任何后续实现若希望恢复“暂缓”内容或突破“收敛”边界，必须先更新产品文档和验收用例。
