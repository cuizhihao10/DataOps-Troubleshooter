# DataOps Troubleshooter 实现原理与学习指南

本文档服务于两个目标：帮助学习者从代码理解 Agent 工程的真实边界；帮助求职者在面试中能够解释每项技术为什么存在、如何实现、如何验证以及当前尚未完成的部分。

文档只描述已经进入仓库并通过验证的实现。尚未完成的模型级 Embedding Provider、GraphRAG 上下文预算、LangGraph Planner/Auditor、长期记忆和完整 API 会明确标记为后续工作，避免把设计当作完成结果。

## 1. 阅读路径

建议按以下顺序阅读代码：

1. `app/domain/`：理解所有跨边界数据为什么必须先经过 Pydantic 校验。
2. `data/fixtures/` 与 `app/core/fixture_registry.py`：理解确定性 Mock 如何保证测试可复现。
3. `mcp_server/` 与 `app/mcp/`：理解真实 MCP 协议边界、工具调用和 Observation 标准化。
4. `app/persistence/`：理解 PostgreSQL、Alembic、pgvector 和显式图表的职责。
5. `app/retrieval/`：理解 Embedding Provider、pgvector/全文双路召回、混合评分、递归图扩展和路径证据。
6. `app/api/main.py`：理解 FastAPI lifespan 如何在对外服务前验证依赖。
7. `tests/`：理解每项设计如何通过失败用例而不是只靠文档保证。

### 1.1 代码注释的强制粒度

本仓库不把文件头说明当作充分注释。模块 docstring 只回答“这个文件为什么存在”，每个
类、函数、异步函数、方法和测试函数还必须单独回答以下问题：

- 输入数据从哪里来，返回值或副作用交给谁。
- 使用的技术机制是什么，以及为什么适合当前边界。
- 哪些校验、预算、白名单或事务规则保证安全性与可重放性。
- 失败会抛出、标准化还是降级，资源如何释放，调用方应观察什么。

复杂函数内部还要在关键步骤旁加入内联注释。这里的“关键步骤”包括外部边界校验、
协议握手、模型转换、重试判断、证据生成、SQL 递归、事务提交/回滚、生命周期资源释放等。
注释应解释顺序和取舍，不能只把下一行代码翻译成中文。

`tests/unit/test_documentation_policy.py` 使用 AST 扫描所有人工编写的 Python 文件，同时检查
模块和 callable 级 docstring。AST 门禁只能保证说明存在并达到最低信息量；注释是否真正
解释原理仍需要代码评审，因此两者共同构成完成定义。

## 2. 分层架构原理

项目把“模型决策”和“确定性执行”严格分开。未来只有 Planner 与 Auditor 是 LLM Agent；输入校验、工具执行、Observation 生成、检索、存储和报告渲染都由普通 Python 节点负责。

这样设计有三个原因：

- 模型擅长在不完整信息下提出假设和选择下一步，但不适合直接控制数据库或构造工具返回。
- 确定性节点可以重放、测试和审计，失败时能够返回明确错误，而不是生成看似合理的自然语言。
- Agent 框架、模型供应商和基础设施可以分别替换，不会把供应商 SDK 传播到领域层。

当前主要目录职责如下：

| 目录 | 职责 | 关键边界 |
|---|---|---|
| `app/domain/` | Pydantic 领域契约 | 不依赖 FastAPI、MCP SDK 或数据库会话。 |
| `app/mcp/` | MCP 客户端、执行和 Observation | Agent 不直接读取 Fixture。 |
| `mcp_server/` | 独立只读 Mock 工具进程 | 只有服务端仓储允许读取合成 Fixture。 |
| `app/persistence/` | SQLAlchemy、Alembic 和种子写入 | 不生成模型结论。 |
| `app/retrieval/` | 知识种子、全文召回和图路径 | 返回节点、边、分数和来源，不直接生成排障报告。 |
| `app/api/` | 服务启动与结构化 HTTP 边界 | 启动成功前检查 Fixture、MCP 和可选数据库。 |

## 3. Pydantic 契约为什么是第一层防线

### 3.1 原理

LLM、HTTP、MCP 和数据库都是不可信边界。即使数据来自本地 Mock，也必须假设字段缺失、类型错误、枚举越界或跨字段组合不合法。Pydantic 模型负责在数据进入领域层前拒绝这些情况。

例如 `PlannerDecision` 不只检查字段类型，还检查组合关系：

- `call_tool` 必须带一个白名单 Action，且不能提前填写停止原因。
- `finish` 和 `need_user_input` 不能携带 Action，并且必须说明停止原因。
- 状态模型中不存在 `Thought` 或 `reasoning_process` 字段，从结构上避免原始思维链进入日志和记忆。

### 3.2 工具统一契约

九个 MCP 工具共享同一输入：资源标识、带时区的时间范围、`scenario_id` 和 `trace_id`。共享输出包含成功标记、结构化数据、证据、错误码、错误信息和观察时间。

统一契约的价值是让 Planner 和后续 LangGraph 节点只处理一种 Observation，不需要了解 LTS、BDS 和 FlashSync 的原始返回差异。

## 4. Fixture 与 Golden Case 的可复现设计

### 4.1 `scenario_id` 驱动

Mock 返回由 `scenario_id` 和工具/资源组合确定，而不是使用随机数。固定输入始终得到固定响应，因此协议测试、Docker 演示和未来评测可以重放同一故障。

当前 Fixture 覆盖：

- 跨组件主键冲突链路。
- BDS 单组件资源压力。
- 空结果。
- 瞬时超时。
- 权限拒绝。
- 服务暂时不可用。

### 4.2 为什么 JSON 文件不写注释

标准 JSON 不支持注释。为了保持 Fixture 能被标准解析器、Pydantic 和其他语言直接读取，文件中不加入非标准注释。字段原理在本文档说明，结构正确性由 `ScenarioFixture` 和单元测试保证。

Golden Case 描述“一个诊断应该做什么”，Fixture 描述“工具会返回什么”。两者分开后，可以在不改变工具响应的情况下调整评测要求，也可以复用一个场景测试不同 Planner 策略。

## 5. MCP 真实协议边界

### 5.1 为什么不能让 Agent 直接读取 Fixture

如果 Planner 节点直接打开 JSON 文件，虽然测试可能通过，但无法证明系统具备标准工具调用能力，也无法测试协议初始化、工具发现、参数 Schema、超时和传输错误。

当前实现使用官方 MCP Python SDK：

1. `mcp_server.server` 启动独立 FastMCP stdio 进程。
2. 服务通过 MCP `list_tools` 暴露九个固定名称、输入 Schema、输出 Schema 和只读注解。
3. `StdioMcpClient` 启动子进程并完成 MCP initialize 握手。
4. 客户端通过 `call_tool` 发送结构化参数。
5. 服务端工具调用 Fixture 仓储，返回经过 Pydantic 校验的统一响应。
6. 客户端解析 `structuredContent`，再由执行器生成 `Evidence` 和 `ToolEvent`。

这条路径确保 Fixture 只存在于 MCP 服务端，Agent 运行时看到的是标准协议 Observation。

### 5.2 只读与安全属性

每个工具都声明：

- `readOnlyHint=true`
- `destructiveHint=false`
- `idempotentHint=true`
- `openWorldHint=false`

协议集成测试读取这些注解，防止以后新增工具时意外变成写操作。项目不会实现自动重跑、删表、扩容或修改同步配置。

### 5.3 重试原理

执行器只对 `TIMEOUT` 和 `SERVICE_UNAVAILABLE` 重试一次。空结果、权限拒绝和非法请求继续重试不会增加信息，因此直接返回。

每次尝试都生成独立 `ToolEvent`，事件 ID 包含 trace、工具名和 attempt 的稳定摘要。即使第二次成功，也保留第一次失败，便于观察延迟、失败率和真实调查过程。

## 6. FastAPI lifespan 与健康检查

FastAPI lifespan 在开始接收请求前执行以下检查：

1. 加载并校验全部 Fixture。
2. 加载 Golden Case，并确认它引用的场景真实存在。
3. 检查版本化 Planner Prompt 是否存在且 ID 匹配。
4. 通过真实 MCP 协议发现九个固定工具。
5. 配置数据库时，建立 PostgreSQL 连接，读取知识节点/边数量，并确认全部节点已位于当前 Provider/维度空间。

任何强依赖不满足时，应用启动失败，而不是在用户提交诊断后才暴露配置错误。未配置数据库的纯单元测试模式会明确返回 `database_status=disabled`；Docker 演示模式必须返回 `database_status=ok`、`knowledge_nodes_embedded=11`，并公开不含凭据的 Provider、维度和评分权重快照。

## 7. PostgreSQL、SQLAlchemy、Alembic 与 pgvector

### 7.1 为什么只使用 PostgreSQL

项目规模较小，PostgreSQL 已能同时承担事务状态、全文索引、向量、图节点/边和案例记忆。引入 Neo4j、Redis 或独立向量数据库会增加部署与面试解释成本，却没有经过用例证明的收益。

### 7.2 SQLAlchemy 的职责

SQLAlchemy 2.x 异步模式通过 `asyncpg` 执行数据库 I/O。领域模型和 ORM Record 分开：领域模型用于边界校验，Record 只负责表映射。这样数据库字段变化不会直接污染 Planner 状态。

### 7.3 Alembic 的职责

Alembic 迁移是数据库结构的版本历史。首个迁移：

- 启用 `vector` 扩展。
- 创建 `knowledge_nodes`。
- 创建 `knowledge_edges`。
- 添加节点类型、关系类型、权重和自环约束。
- 添加外键、唯一约束、普通索引与全文 GIN 索引。

容器启动顺序是数据库健康检查通过后，先执行 `alembic upgrade head`，再执行幂等种子写入，最后启动 API。

### 7.4 pgvector 当前边界

原始人工知识 JSON 的 embedding 仍为 `null`，因为静态种子不应固化某个 Provider 的派生向量。容器执行 `app.persistence.seed` 时，根据当前配置批量生成向量，并在同一事务中写入 `embedding`、`embedding_provider` 和 `embedding_dimensions`。

第二个迁移使用 CheckConstraint 保证向量和两项溯源元数据同时存在或同时为空，并验证 `vector_dims(embedding)` 等于记录维度。查询先按 Provider ID 和维度过滤，再由 pgvector cosine distance 运算符排序，因此模型或维度切换后不会把不兼容空间混在一起。

### 7.5 可替换 Embedding Provider

`app/retrieval/embeddings.py` 定义异步 `EmbeddingProvider` 协议：实现只需提供稳定 `provider_id`、固定 `dimensions` 和保持顺序的批量 `embed_texts`。数据库仓储和融合服务不导入任何模型 SDK，未来可以替换成 OpenAI-compatible 或本地模型实现。

默认 `deterministic-hash:v1` 使用 NFKC 规范化、英文词元/字符三元组、中文单字/二元组/三元组和 SHA-256 feature hashing，再执行 L2 归一化。它的优点是无网络、无凭据、跨进程可重放，适合测试和作品演示；限制是没有神经模型级同义词理解，因此 README 不把它宣传成高质量通用语义模型。

Provider 算法发生变化时必须提升 ID 版本。只改变维度则更新 `embedding_dimensions`；两者都会让旧行自动退出当前向量查询，直到重新执行幂等种子写入。

## 8. 显式 GraphRAG 路径

### 8.1 为什么关系必须进入边表

如果把“LTS 依赖 BDS，BDS 依赖 FlashSync”只写在一段文本里，检索系统只能返回相似文档，无法可靠证明链路节点和边是否完整。显式边表允许系统返回：

```text
component_lts
  -[DEPENDS_ON]-> component_bds
  -[DEPENDS_ON]-> component_flashsync
```

每条边保存来源 ID 和原文跨度，最终报告可以引用 `path_id` 并回溯到人工知识种子。

### 8.2 全文种子召回

PostgreSQL `to_tsvector` 和 `websearch_to_tsquery` 召回全文种子，同时使用名称/别名包含匹配补充短标识符。另一条 SQL 使用 pgvector cosine distance 召回相同 Provider/维度的向量种子；两条查询均执行数据库 top-k，不在 Python 中加载全表计算距离。

服务按 `node_id` 合并两路候选，保留 `lexical` / `vector` 命中通道和原始分量。全文 ts_rank/bonus 被裁剪到零到一，cosine similarity 同样标准化；单路未命中时对应分量为零，而不是复制另一通道分数。

### 8.3 五项混合评分

默认权重与产品基线一致：语义 0.45、全文 0.10、路径 0.25、可靠性 0.10、案例新鲜度 0.10。`HybridScoringWeights` 强制总和为 1，环境变量可以逐项覆盖，但错误总和会阻止 Settings 构造。

种子尚无路径分，因此种子分只包含语义、全文、可靠性和当前可用的新鲜度；图扩展后再加入 `GraphPath.score × path_weight` 得到最终 `ScoredGraphPath.hybrid_score`。原始边权乘积分与最终混合分分开保存，调权不会覆盖真实路径关系。当前人工知识节点没有案例时间字段，freshness 明确为零；长期案例切片接入时间戳后再使用该项。

### 8.4 递归 CTE 路径扩展

路径扩展使用 PostgreSQL `WITH RECURSIVE`：

1. 从种子节点选择白名单关系的第一跳边。
2. 将目标节点追加到 `node_ids` 数组，将边追加到 `edge_ids`。
3. 在深度小于预算时继续下一跳。
4. 如果目标节点已经出现在路径中则停止，避免环。
5. 路径分数是各边权重乘积，弱关系会降低整条路径得分。

最大跳数限制为 1 或 2，与产品预算一致。`path_id` 由有序 edge ID 计算稳定 SHA-256 摘要，同一条路径在重放时保持相同引用。

### 8.5 删边消融为什么重要

集成测试先验证能够得到 LTS → BDS → FlashSync 两跳路径，再在事务中删除 BDS → FlashSync 关键边。删除后相同查询不能返回三组件路径，事务随后回滚。

这个测试证明答案依赖真实图关系；如果删边后结果完全不变，说明所谓 GraphRAG 可能只是类名或提示词包装。

## 9. 测试分层

- 单元测试：Pydantic 约束、Fixture、Prompt Schema、Observation、Provider 稳定性、向量元数据和混合评分公式。
- MCP 集成测试：真实 stdio 握手、九工具发现、成功/失败响应和重试 trace。
- PostgreSQL 集成测试：迁移、pgvector 扩展、带 Provider 溯源的幂等种子、cosine/全文双路检索、混合评分、递归路径和删边消融。
- Docker 验证：从镜像安装依赖，等待 PostgreSQL 健康，执行迁移/种子，再检查 API `/health`。

PostgreSQL 测试使用 `postgres` marker。普通 `pytest` 默认排除它，保持无 Docker 环境下的快速反馈；显式数据库验证使用：

```powershell
$env:DATAOPS_TEST_DATABASE_URL='postgresql+asyncpg://...'
python -m pytest -m postgres
```

## 10. 配置与生成文件说明

| 文件 | 为什么不逐行注释 | 如何理解和验证 |
|---|---|---|
| `requirements*.lock` | 由 pip-tools 机械生成，手工注释会在再生成时丢失。 | 依赖来源在 `pyproject.toml`，一致性由 `pip check` 和 Docker 构建验证。 |
| `data/fixtures/**/*.json` | 标准 JSON 不允许注释。 | Pydantic Scenario Schema 和 Fixture 测试。 |
| `data/knowledge/*.json` | 需要被标准加载器和其他语言读取。 | `KnowledgeSeedBundle`、source_span 校验和 PostgreSQL 集成测试。 |
| PNG / DOCX | 二进制格式不能可靠保存代码式注释。 | Markdown 产品基线、本文档和正式阅读版正文。 |

## 11. 当前完成度与下一步

已经完成：

- 契约与 Fixture 基线。
- 九个真实 MCP 只读 Mock 工具。
- Action → MCP → Observation 与单次瞬时错误重试。
- PostgreSQL/pgvector 图存储基础。
- 人工知识种子、可替换 Embedding Provider、真实 pgvector cosine 查询。
- 全文/向量种子合并去重、五项可解释评分和 1–2 跳显式路径扩展。

尚未完成：

- 模型级 Embedding Provider（当前默认实现是离线 feature hashing 基线）。
- GraphRAG evidence bundle 上下文预算。
- 五个运行时 capabilities。
- LangGraph Planner ReAct / Auditor 双 Agent。
- 长期记忆确认、去重和召回。
- 28 个 Golden Cases 和消融报告。
