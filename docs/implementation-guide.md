# DataOps Troubleshooter 实现原理与学习指南

本文档服务于两个目标：帮助学习者从代码理解 Agent 工程的真实边界；帮助求职者在面试中能够解释每项技术为什么存在、如何实现、如何验证以及当前尚未完成的部分。

文档只描述已经进入仓库并通过验证的实现。尚未完成的模型级 Embedding Provider、完整诊断 API、
会话 checkpoint 和案例到 GraphRAG 的自动注册会明确标记为后续工作，避免把设计当作完成结果。

## 1. 阅读路径

建议按以下顺序阅读代码：

1. `app/domain/`：理解所有跨边界数据为什么必须先经过 Pydantic 校验。
2. `data/fixtures/` 与 `app/core/fixture_registry.py`：理解确定性 Mock 如何保证测试可复现。
3. `mcp_server/` 与 `app/mcp/`：理解真实 MCP 协议边界、工具调用和 Observation 标准化。
4. `app/persistence/`：理解 PostgreSQL、Alembic、pgvector 和显式图表的职责。
5. `app/retrieval/`：理解 Embedding Provider、pgvector/全文双路召回、混合评分、递归图扩展和路径证据。
6. `app/memory/`：理解审计门禁、pending 候选、两阶段去重、确认决策与 confirmed-only 召回。
7. `app/api/main.py`：理解 FastAPI lifespan 如何在对外服务前验证依赖。
8. `tests/`：理解每项设计如何通过失败用例而不是只靠文档保证。

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

项目把“模型决策”和“确定性执行”严格分开。只有 Planner 与 Auditor 是 LLM Agent；输入校验、工具执行、Observation 生成、检索、报告草稿/修订、存储和渲染都由普通 Python 节点负责。

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
| `app/memory/` | 受控长期案例记忆 | 只有 accepted 报告可暂存；默认召回只读取 confirmed。 |
| `app/reporting/` | 确定性草稿、规则门禁和安全修订 | 不调用模型，不新增事实，不执行修复。 |
| `app/agents/` | Planner 与独立 Auditor 的 Prompt/Provider | 只返回结构化决策，不直接调用 MCP 或写数据库。 |
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

每次尝试都生成独立 `ToolEvent`，事件 ID 包含 trace、工具名、规范化请求和 attempt 的稳定摘要。即使第二次成功，也保留第一次失败，便于观察延迟、失败率和真实调查过程；同一工具查询不同资源或时间窗时也不会发生 ID 冲突。

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

### 8.6 Evidence Bundle 上下文预算

`GraphRetrievalResult` 是完整检索结果，不应原样注入 Planner。`app/retrieval/budget.py` 将它转换为 `graphrag-evidence-bundle:v1`：先按最终检索排序尝试加入路径及其全部节点，再补充未出现的高分种子。路径是原子候选，任何字节、节点或路径预算不满足时整条省略，不会切断边或截短正文。

默认预算为 6000 个 UTF-8 JSON 字节、8 个唯一节点和 4 条路径。使用字节而不是某个供应商 tokenizer，能在尚未绑定模型时保持精确可重放；未来模型适配层仍可在此硬上限内增加供应商 token 检查。`used_bytes` 只统计规范序列化后的 selected_nodes/selected_paths 主体，omitted IDs 属于诊断元数据。

知识节点使用 `kn_<node_id>` 作为稳定证据引用，路径继续使用数据库边序列生成的 `path_id`。Bundle 不包含 embedding、模型原始推理或数据库内部状态；`truncated=true` 明确表示仍有候选因预算未注入。

### 8.7 Vector-only / Vector+Graph 消融

检索服务提供三个显式模式：`vector_only` 只返回向量种子，`vector_graph` 在相同向量种子上扩图，`hybrid_graph` 再加入全文通道并作为生产默认值。模式进入 `graphrag-retrieval:v2` 输出，避免通过隐藏布尔开关运行无法复现的实验。

`data/evals/graphrag_ablation_cases.json` 使用稳定知识节点 ID 标注预期根因和必要有序路径。`app/retrieval/ablation.py` 计算根因节点是否可见，以及必要节点序列在最佳真实路径中的覆盖比例；它不调用 LLM，因此当前指标只描述检索层，不冒充最终报告准确率。

首个案例实测值记录在 `docs/graphrag-ablation-results.md`。结果显示根因在 vector-only 已经命中，图扩展没有虚报额外根因收益；图的可解释增益体现在必要因果链完整率从 0.0 变为 1.0。

## 9. 固定 runtime capability registry

### 9.1 capability 与 Agent、Codex Skill 的边界

`app/capabilities/` 是运行时领域策略层，不是 `.agents/skills/` 中指导 Codex 开发仓库的 Skill，
也不是第三种 LLM Agent。五项 capability 只保存四类声明式数据：Prompt 片段、MCP 工具建议
优先级、上游必须提供的输入字段、下游必须执行的输出校验规则。数据模型没有 handler、callback、
LLM client 或 MCP client 字段，因此定义本身无法发起 I/O 或绕过 Planner ReAct。

五项能力的职责分别是：

1. 单组件诊断：把调查面限制在唯一组件，并按状态、日志、组件元数据的顺序减少无效 Action。
2. 跨组件链路溯源：结合拓扑、实时 Observation 和 GraphRAG `path_id` 逐段验证传播关系。
3. 历史案例匹配：只使用 confirmed 案例，输出共同点、差异点、参考方案和避坑提示。
4. 风险评估：要求每项建议提供风险等级、前置条件、回滚和验证，不执行生产写操作。
5. 结构化报告：固定摘要、链路、根因、证据、修复、风险、不确定性和相似案例字段。

### 9.2 为什么使用固定 registry

`CapabilityRegistry` 的构造函数不接受外部定义，而是审计代码中固定的五项集合。这样新增或删除
能力必须经过产品文档、代码和测试变更，不能从配置或网络动态注入未审查策略。内部使用只读
`MappingProxyType`，定义和选择结果使用 frozen Pydantic 模型，防止请求之间就地修改共享策略。

选择调用链如下：

```text
上游路由产生 intent + components + history_trigger
  -> CapabilitySelectionRequest 校验组件数量和去重
  -> CapabilityRegistry 选择单组件或跨组件主能力
  -> 仅在显式 trigger 下追加 history
  -> 始终追加 risk + reporting
  -> 稳定去重工具、输入和规则
  -> runtime-capabilities:v1 进入 Planner {active_capabilities}
```

单组件意图必须恰好一个组件；registry 根据工具固定的 `<component>.<operation>` 命名空间过滤
另外六个工具。跨组件意图至少两个组件，并只保留所选组件工具的链路调查顺序；三个组件都在
范围内时才会得到完整九工具列表。注册表不根据用户自然语言猜意图，因为这会把路由职责和
不可解释的关键词规则混入策略组合。

### 9.3 历史触发和证据优先级

历史匹配默认是 `not_requested`，只有 `user_requested`、`planner_validation` 或
`reusable_signature` 才加入。这个枚举与 `docs/prompt-contracts.md` 保持一致，选择结果保存触发
来源供后续事件审计。当前切片只实现策略契约，不假装已经实现长期记忆数据库召回。

历史 Prompt 和输出规则同时要求 confirmed 过滤与“实时 Observation 为准”。双重声明是为了让
未来 Planner 和 Auditor 共享同一安全边界：相似度只能找到候选，不能把旧案例自动升级成本次
根因；发生冲突时必须写入 differences 并保留实时证据。

### 9.4 启动审计、失败路径和验证

FastAPI lifespan 构造默认注册表、校验配置中的 `runtime-capabilities:v1`，并在 `/health`
公开固定能力名称和契约版本。定义重复、缺失、额外增加、契约 ID 漂移、单/跨组件范围错误都会
显式失败，不会静默选择近似策略。单元测试覆盖五项集合、BDS 工具过滤、三类历史触发、实时
证据优先文案、非法组件组合和无执行钩子 Schema；健康集成测试覆盖启动接线。

## 10. LangGraph 有界 ReAct

### 10.1 为什么必须使用真实状态图

`app/orchestration/react_loop.py` 使用 LangGraph 1.x `StateGraph` 编译固定拓扑，而不是在一个
while 循环中手工模仿节点名称。依赖通过 `pyproject.toml` 声明为 `langgraph>=1.2,<2`，当前
锁文件解析为 1.2.2；锁文件由 pip-compile 生成，不手工编辑传递依赖。

图的最小闭环是：

```text
START
  -> select_capabilities
  -> planner_react
       -> execute_tool
       -> planner_react
       -> END
```

`select_capabilities` 调用固定 registry，并把意图和名称注入 AgentState；`planner_react` 调用
可替换 `PlannerAgent` 协议，接收结构化 PlannerDecision；`execute_tool` 使用注入执行器跨真实
MCP，随后原子回写 Evidence、ToolEvent、observation_refs 和 react_step。

LangGraph 的 state_schema 使用 `ReactGraphState` Pydantic 模型。每个节点接收和返回强类型
模型，框架最终给出的映射也立即通过 `model_validate` 重建。Planner、Executor、Registry 和
截止时间通过 `context_schema=ReactGraphRuntime` 注入，不进入 checkpoint，也不会与并发运行
共享可变状态。

### 10.2 Planner 协议为何不是占位 Agent

`app/agents/planner.py` 的 `PlannerAgent` 是依赖反转边界：生产实现必须接收
`PlannerTurnContext` 并返回 `PlannerDecision`。它不提供工具执行或 Observation 写入方法，因而
模型供应商适配器不能绕过图节点。OpenAI-compatible Chat Provider、v2 Prompt Renderer 和一次
结构化输出修复现已实现；报告草稿和独立 Auditor 由后续 `audited-report-workflow:v1` 接续。
Scripted Planner 测试仍用于隔离纯图控制流，官方 SDK MockTransport 测试则验证真实模型协议边界。

PlannerTurnContext 会再次检查 AgentState.intent、active_capabilities 和 CapabilitySelection
一致，并拒绝预算耗尽后的模型调用。remaining_time_ms 来自控制器的单调时钟截止时间，模型只能
看到剩余预算，不能自行延长。

### 10.3 Action 门禁与重复检测

PlannerDecision 通过 Pydantic 只证明 JSON 结构合法，仍不足以安全执行。控制器在 MCP 前依次
检查：引用是否存在、工具是否属于当前组件范围、trace_id 是否等于 run_id、Action 是否已经
执行。任何失败都生成 `policy_blocked` 事件和公开 stop_reason，不调用 Executor。

重复检测将完整 ToolAction 规范化为排序键、紧凑分隔符的 UTF-8 JSON，再计算 SHA-256。参数
中包含资源、时间窗、场景和 trace；因此真正同参会被拦截，同工具不同资源仍允许执行。恢复已有
AgentState 时，控制器从 ToolEvent 重建指纹，checkpoint 不能成为重复调用绕过路径。

MCP 内部 TIMEOUT/SERVICE_UNAVAILABLE 重试仍由 McpToolExecutor 负责。一次 Planner Action
无论产生一个还是两个 ToolEvent，react_step 都只增加一；这样“最多 6 步”表达调查决策预算，
不会被传输重试歪曲，同时总网络尝试仍完整可审计。

### 10.4 总超时与最后完整状态

`DATAOPS_REACT_TOTAL_TIMEOUT_SECONDS` 默认 60 秒，覆盖 LangGraph 调度、Planner 等待和 MCP
执行，独立于单工具 timeout。控制器使用 `asyncio.timeout` 取消超时节点，并以 LangGraph
`astream(stream_mode="values")` 持续保存最后一个完整 Pydantic 状态。这样第二次工具卡住时，
第一次已经取得的证据不会因为总超时丢失；终态追加 `total_timeout`，但不会伪造失败节点结果。

递归上限按 `max_steps * 2 + 6` 设置，覆盖路由、每次 execute/planner 回边和最终预算检查。
它是框架死循环的第二道防线，业务停止仍由 react_step 和 stop_reason 决定。

### 10.5 公开事件与审计 ID

`ReactPublicEvent` 只记录稳定 ID、序号、类型、公开摘要、工具名、Observation 引用和停止原因，
不包含 Thought。终止类事件强制 stop_reason；`ReactRunResult` 强制最终 AgentState 和最后事件都
处于可解释终态，防止条件边错误导致无声结束。

Evidence 与 ToolEvent 的稳定 ID 现在包含规范化请求身份。此前同一 trace 内同一工具查询不同
资源可能共享事件 ID；加入资源、时间窗、场景和 trace 后，不同参数调用可独立寻址，而完全相同
请求重放仍稳定。合并状态时若同 ID 的结构化载荷不同，控制器显式失败，不覆盖旧审计事实。

### 10.6 验证范围

单元测试覆盖 capability 注入、Action/Observation 回写、同参拦截、组件越界、trace 漂移、
无效证据引用、步数耗尽、总超时和不同参数审计 ID。集成测试用 Scripted Planner 发出
`lts.get_task_status`，Action 必须经过 LangGraph 和真实 stdio MCP，再由第二轮 Planner 读取
回写证据并 finish。该测试证明控制器闭环真实存在，但不宣称付费模型推理质量。

## 11. OpenAI-compatible Planner Structured Outputs

### 11.1 为什么使用官方 SDK 的 Pydantic parse

`app/agents/chat.py` 使用 `AsyncOpenAI.chat.completions.parse`，把 `PlannerDecision` 类直接作为
`response_format`。SDK 自动生成 strict JSON Schema，并把 assistant content 解析回同一 Pydantic
类型；项目无需手写第二份 JSON Schema。官方 Structured Outputs 文档明确建议优先于 JSON mode，
并建议使用 Pydantic/Zod 原生类型避免代码与 Schema 漂移。

虽然 OpenAI 最新模型指南更推荐复杂 reasoning/tool workflow 使用 Responses API，本项目的模型
不直接调用 API tools：LangGraph 和 MCP 已经拥有确定性工具循环，Planner 单次职责只是返回一个
结构化决策。因此选择广泛兼容的 Chat Completions Structured Outputs 作为 OpenAI-compatible
边界，便于 GPT、Qwen、DeepSeek 等兼容端点替换；不支持 strict json_schema 的端点会显式失败，
不会静默降级成自由文本。

依赖在 `pyproject.toml` 中声明为 `openai>=2.45,<3`，当前锁定 2.45.0。Provider 设置
`max_retries=0`，避免 SDK 自动重试隐藏真实调用次数或突破 LangGraph 总超时。

### 11.2 v2 Prompt 的 system/user 隔离

`planner-react:v2` 使用两个独立文件：system 只包含角色、安全和输出行为；user 承载查询、计划、
capability、工具 Evidence、GraphRAG Bundle、路径、confirmed 案例、允许工具和预算。用户 query
先转成 JSON 字符串，因此换行和伪造章节仍只是低优先级 user 数据，不会插值到 system 消息。

`PlannerPromptRenderer` 在构造时用 `string.Formatter` 精确审计占位符集合。新增或删除字段必须同步
代码和 Prompt，否则 Agent 构造立即失败。所有 Pydantic 载荷使用排序键、保留中文的 UTF-8 JSON，
同一上下文可重放；缺少 GraphRAG 明确渲染为 null，缺少历史案例为 []，不伪造已执行检索。

工具上下文只投影 Evidence、终态数据、错误分类、尝试次数和 observation_refs。九个工具共享一个
McpToolRequest Schema，允许名称按 capability 裁剪，避免重复九份相同 Schema。PlannerDecision
Schema 通过 API response_format 提交，不在 Prompt 中重复，减少 token 与漂移风险。

### 11.3 一次受控 Schema 修复

首次 SDK/Pydantic ValidationError 会提取最多十个字段错误和截断 assistant 原输出。原输出只在
当前 decide 调用内作为第二轮 assistant 消息回放，随后追加 user 指令：“只修复 JSON/字段组合，
不得增加事实、Observation、Markdown 或 Thought”。第二次请求仍附带同一 strict Schema。

修复预算只能是 0 或 1。第二次失败转换为 attempts=2 的 `planner_output_invalid`，绝不递归第三次。
异常字符串只含安全摘要，raw output 不进入 AgentState、ReactPublicEvent、日志或 API。refusal 不属于
格式错误，直接形成 `planner_refusal`；timeout、连接、限流、认证和服务错误形成
`planner_provider_error`，错误映射不复制响应体或完整 URL。

### 11.4 配置、SecretStr 与资源释放

Settings 默认 `chat_provider=disabled`，所以无 key 的快速测试和 Docker 演示仍可启动。启用
`openai-compatible` 时必须提供 `DATAOPS_CHAT_API_KEY`；它使用 SecretStr，base_url 禁止包含
username/password。默认模型 `gpt-5.6` 来自实现时官方 latest-model 指南，可由环境覆盖。

FastAPI lifespan 调用 `create_planner_runtime`：disabled 返回 None；启用时构造 Provider/Agent 但不
发送付费健康探测。`/health` 只报告 disabled/configured、Provider、模型、endpoint host、超时和
修复预算；configured 仅表示本地配置可构造，不冒充远端服务已连通。退出时关闭自有 AsyncOpenAI
连接池，注入测试客户端由测试自行管理。

### 11.5 验证范围

单元测试覆盖 Prompt injection 角色隔离、工具裁剪、空检索/记忆、合法输出、一次修复、二次失败、
refusal、disabled/key/URL 配置和 SecretStr。SDK 集成测试用真实 AsyncOpenAI + MockTransport 检查
strict Schema、无 API tools、ValidationError、refusal 和 timeout；完整集成测试再贯通模型
Action → LangGraph → 真实 stdio MCP → Evidence → 模型 finish，全程不访问外部付费模型。

## 12. 确定性报告草稿与 Auditor Structured Outputs

### 12.1 为什么草稿由确定性 Builder 生成

Planner 的职责是调查和维护结构化假设，不应在停止时顺便生成一大段不可审计文本。
`app/reporting/draft.py` 把终态投影为 `DiagnosisReport`：只提升 `supported/confirmed` 且拥有有效
supporting evidence、没有 contradicting evidence 的假设；GraphRAG 完整路径转换为带 `path_id`
的 `FaultChainStep`；只有 solution/SOP 节点能生成有知识引用的修复建议。没有方案证据时只返回
低风险只读补证步骤。

`FaultChainStep` 解决旧 `list[str]` 无法逐段审计的问题。`RemediationStep` 新增 evidence_refs，
high 风险在 Pydantic 层同时强制引用和 prerequisites；`DiagnosisReport` 还要求修复步骤从 1 连续
编号。这些结构约束先于模型，不能被 Prompt 忽略。

### 12.2 确定性规则为何拥有否决权

`app/reporting/policy.py` 汇总实时 Evidence、GraphRAG 节点/路径和 confirmed case 引用，逐项检查
悬空 ID、报告级汇总遗漏、根因是否对应支持假设、反对证据、风险保护和案例确认状态。模型即使
返回 accept，只要规则问题非空，`_merge_audit_result` 仍强制 revise。

这不是用规则替代 Auditor。规则擅长“ID 是否存在”“状态是否 confirmed”“字段是否齐全”；Auditor
负责“引用文本是否真的支持结论”“实时结果与历史/知识是否语义冲突”。两层结合比单独依赖任一层
更可解释。

### 12.3 独立 Auditor Provider 与 Prompt

`auditor-report:v1` 与 Planner 一样拆成静态 system 和不可信 user 数据。Renderer 注入报告、实时
Evidence/ToolEvent、GraphRAG Bundle、confirmed cases、capability 规则、确定性 issues 和审计轮次。
Auditor 只能返回 `AuditResult` 的 accept/revise，有限 `AuditIssueCode` 防止模型发明新控制状态。

`app/agents/auditor_chat.py` 使用真实 AsyncOpenAI
`chat.completions.parse(response_format=AuditResult)`，不传 API tools。Schema 格式失败最多修复一次；
refusal、timeout、连接和 HTTP 错误不修复、不默认 accept。原始无效输出只在当前调用内回放并截断，
不会进入 AgentState、公开事件或日志。

Planner 与 Auditor 使用同一 Chat 配置但各自拥有 Prompt ID、Provider contract 和 schema repair
预算。默认 Provider disabled；FastAPI lifespan 只构造本地运行时，不发付费健康探测，退出时分别
关闭自有连接池。`/health` 公开两个角色的 disabled/configured、模型、host、超时和修复预算，不
公开 key 或完整认证 URL。

### 12.4 最多一次报告级返工与安全降级

`app/orchestration/report_workflow.py` 编译独立 LangGraph：draft → audit → accept，或 revise →
safe revision → audit。`max_audit_revisions` 默认一，与“Schema 修复一次”不同：Schema 修复只更正
AuditResult JSON，报告返工会产生新 `DiagnosisReport` 并再次审计。

`SafeReportReviser` 只过滤悬空引用、删除不支持/冲突根因、移除未确认案例并把风险建议收窄为只读
补证，不会增加事实或提高置信度。第二轮仍 revise、预算为零、Provider/refusal/二次 Schema 失败时，
工作流生成安全降级报告：清空根因、链路和案例结论，保留可寻址证据索引与只读下一步，并明确
禁止生产写操作。degraded 不是 accepted，后续长期记忆节点必须据此拒绝写入。

当前返工只覆盖报告级收窄；如果 Auditor 发现必须补充新的实时 Observation，本切片返回降级和
只读补证步骤，尚未把边重新接回 Planner ReAct。这样明确保留范围，而不是用同一旧证据重复推理。

### 12.5 验证范围

单元测试覆盖 Pydantic high-risk/accept-revise 不变量、确定性 Builder、语义不支持否决、保守修订、
Prompt 角色隔离、一次 Schema 修复、Provider 失败、一次返工与二次降级。集成测试用真实
AsyncOpenAI + MockTransport 检查 strict AuditResult Schema、无 tools，并让 Mock 模型两次返回
accept：第一次仍被 deterministic issue 否决，修订删除根因后第二次才通过。测试不访问付费模型，
不宣称 Auditor 质量指标。

## 13. 受控长期案例记忆

### 13.1 为什么采用“审计通过后暂存”，而不是自动学习

长期记忆会跨会话影响后续 Planner，因此一次错误写入比一次错误回答危害更持久。
`CaseMemoryService.stage_from_report` 只接收 `ReportRunResult`：只有 workflow outcome 为
`accepted`、`AuditResult.status=accept` 且最终报告至少包含一个根因时才构建候选。`degraded`、
`revise`、Auditor 不可用和无根因安全报告都返回结构化 skipped 状态，不调用仓储写方法。

合法候选仍不会直接成为可信知识。新记录固定以 `pending` 创建，等待用户通过
`POST /api/v1/memories/{memory_id}/confirm` 发送 `confirm` 或 `reject`。这个门禁把“模型输出经过
审计”和“用户同意跨会话复用”拆成两个独立事实，避免把 Auditor 当成业务批准人。

### 13.2 候选投影、签名和向量边界

候选只保存报告已公开的结构化字段：症状、canonical 根因、故障路径、方案、组件、标签和
Evidence 引用；不保存 Planner/Auditor 原始输出或思维链。`CaseMemory` 是对外领域模型，embedding
只存在于内部 `StoredCaseMemory` 和数据库列，因而 API、Prompt 与日志不会意外输出高维数组。

精确签名由排序去重后的组件集合和轻量规范化根因组成，再计算 SHA-256。规范化只统一大小写、
首尾和连续空白，不做激进中文同义词折叠：这样 exact signature 处理“同一事实的稳定重放”，
语义近似但措辞不同的案例交给向量阶段，避免把不同根因过早合并。

### 13.3 advisory lock 与 exact → vector 两阶段去重

去重范围使用排序组件集合生成 PostgreSQL transaction advisory lock。锁必须先于 exact/vector
查询获取，并覆盖主表更新和 `memory_evidence` 关联写入；事务 commit 或 rollback 时自动释放。
如果两个请求同时暂存同一组件故障，后到事务会在前一个完成后重新查重，而不是都观察到“无记录”
并插入重复行。数据库唯一签名约束仍作为最后一道防线，但不能替代正确的并发顺序。

第一阶段按 exact signature 查询。命中时无需调用 Embedding Provider，降低成本和故障面。只有未
命中才生成候选向量，并在组件完全相同、Provider ID 相同、向量维度相同的记录中执行
pgvector cosine 相似度查询；默认阈值为 0.92。Provider/维度隔离很重要，因为不同模型产生的坐标
空间没有可比较语义，即使数组长度偶然相同也不能混算距离。

### 13.4 合并、same run idempotency 与 `memory_evidence`

重复案例保留旧记录的 `memory_id`、canonical `root_cause`、确认状态和 signature，只稳定去重合并
症状、路径、方案、组件、标签和 Evidence。这样一次措辞不同的新报告不会改写已被用户确认的根因，
也不会把 confirmed/rejected 悄悄恢复为 pending。合并后的展示文本重新生成 embedding，使向量仍
描述当前完整案例，而不是最初版本。

`memory_evidence(memory_id, evidence_ref, source_run_id)` 使用复合主键保存审计来源。Service 在增加
`occurrence_count` 前先查询同一 memory/run 是否已经关联：same run idempotency 保证 HTTP 重试、
任务重放或事务恢复不会重复计数。新 run 只写本次候选实际携带的 Evidence；历史合并字段中的旧
引用不会复制成新 run 的来源，否则审计链会错误声称旧证据由本次诊断观察到。

主记录、出现次数和 Evidence 关联共享一个 `AsyncSession.begin()`。唯一约束、CheckConstraint、
Provider、Pydantic 或 SQL 任一步失败都会回滚整个事务，不允许出现“计数已增加但来源关联缺失”或
“主记录已插入但向量不可用”的部分成功。

### 13.5 confirm、reject、重新 confirm 与 confirmed-only 搜索

状态决策是显式有限枚举：`confirm` 映射 `confirmed`，`reject` 映射 `rejected`。同一目标状态幂等；
confirmed 与 rejected 可以相互切换以支持取消确认和纠错，但 API/Service 不提供恢复 pending 的
隐式动作。不存在的 memory 返回 404；数据库未配置时两个记忆 API 都返回 503，明确区别“能力未
启用”和“搜索成功但没有命中”。

搜索先校验非空 query 和 1..20 的 limit，再使用当前 Provider 生成单个固定维度向量。仓储 SQL
在数据库层同时过滤 `status='confirmed'`、Provider 和维度，响应模型再次拒绝非 confirmed 案例，
形成纵深防御。搜索结果只返回公开 `CaseMemory`、cosine 相似度和 `case-memory:v1`，不返回向量。
Planner/历史 capability 使用这些结果时仍必须让本次实时 Observation 优先，历史案例只能作为参考。

如果部署切换 Embedding Provider，旧案例不会跨空间参与搜索；当前切片没有批量重嵌入迁移。精确
签名再次命中某条旧案例时会用当前 Provider 重算该条向量，但完整 Provider 切换仍应先提供显式的
离线重嵌入与审计命令，不能直接修改 Provider ID 冒充向量兼容。

### 13.6 生命周期、健康检查与当前范围

FastAPI lifespan 仅在 PostgreSQL 配置并通过连接/种子检查后创建 `PostgresMemoryRuntime`。Runtime
只保存 session factory、Embedding Provider 和预算；每次写入创建独立事务，每次查询创建短只读
会话，避免跨请求共享非线程安全 `AsyncSession`。`/health` 报告契约、Provider、维度、去重阈值、
默认 limit 及 pending/confirmed/rejected 计数，但不公开数据库 URL、密码或 embedding 内容。

本切片已经实现并验证候选构建、暂存/合并、两阶段去重、同 run 幂等、确认/拒绝/重新确认、默认
搜索和 API 降级语义。顶层 `AuditedDiagnosisWorkflow` 已在内部调用链中把 report 终态自动交给
`runtime.stage()`；但完整诊断 HTTP API 尚未存在。已确认案例也尚未自动注册为 GraphRAG `case`
节点或建立 `SIMILAR_TO`，历史案例的共同点/差异点/避坑提示仍需后续投影，删除 API 也尚未实现。
这些边界不能描述为已经完成。

### 13.7 验证方式

- 单元测试覆盖审计资格、无根因跳过、exact/vector 合并、same run 幂等、canonical 字段保留和状态可见性。
- API 集成测试覆盖数据库禁用 503、confirm/reject/search Schema 和健康计数刷新。
- PostgreSQL 集成测试真实执行 Alembic、advisory lock 范围、pgvector cosine、复合来源关联和数据库约束。
- `tests/unit/test_documentation_policy.py` 把 Service、Repository、Runtime 和迁移列为关键边界文件，要求 callable docstring 与关键步骤内联注释同步存在。

```powershell
$env:DATAOPS_TEST_DATABASE_URL='postgresql+asyncpg://...'
python -m pytest -q tests/unit/test_case_memory_service.py tests/integration/test_memory_api.py
python -m pytest -q tests/integration/test_case_memory_postgres.py
```

## 14. 端到端诊断编排

### 14.1 为什么还需要一个顶层工作流

`BoundedReactLoop` 和 `AuditedReportWorkflow` 分别解决调查与审计，但单独调用它们无法保证历史
召回发生在 Planner 前、Auditor 使用同一历史上下文、记忆写入发生在审计后。若这些顺序散落在
未来 API 路由中，测试很难证明没有某个入口提前写入或漏掉召回。

`audited-diagnosis-workflow:v1` 因此使用第三层确定性 LangGraph 组合四个节点：

```text
recall_case_memories -> run_react -> run_report -> stage_case_memory
```

它不是第三个 Agent。节点只调用现有协议对象，Planner 与 Auditor 仍是唯二 LLM 角色；顶层状态只
保存 Pydantic 请求、查询、confirmed matches 和三个子结果，Provider、数据库 session factory 等
不可序列化依赖通过 LangGraph runtime context 注入。

### 14.2 历史按需触发和查询预算

`CapabilitySelectionRequest.history_trigger` 是唯一查询开关。`not_requested` 直接跳过数据库；
`user_requested`、`planner_validation`、`reusable_signature` 才调用记忆 runtime。这样历史匹配仍是
第五项按需 capability，而不是每个诊断固定支付的向量查询。

查询由 `_build_memory_query` 确定性构造，顺序是用户问题、本次非 CASE_MEMORY Evidence、当前假设。
实时 Observation 位于假设之前，使字符预算截断时优先保留本次事实；CASE_MEMORY 来源被排除，避免
旧案例内容递归查询并强化自身。`memory_query_max_chars` 默认 4000，`memory_search_limit` 默认 5，
都由 Pydantic 限制并在 `.env.example` 说明。

### 14.3 同一 confirmed 上下文贯穿 Planner 与 Auditor

记忆搜索返回 `CaseMemoryMatch(memory, similarity)`，响应模型已拒绝 pending/rejected。顶层把其中
的 `memory` 投影为同一 tuple，分别构造 `ReactRunRequest` 和 `ReportRunRequest`；两个子模型再次
检查 status=confirmed。这样 Planner 不能看到 Auditor 未看到的历史事实，确定性报告规则也能检查
最终类似案例引用是否来自本轮确认上下文。

similarity 目前保留在顶层 `DiagnosisRunResult.recalled_memories`，尚未进入 Planner Prompt；生产
Builder 也尚未生成 `SimilarCaseReference` 的共同点/差异点。因此本切片证明“召回并安全注入”，
不宣称已经完成历史对比文本生成。

### 14.4 审计后 staging 与失败语义

`stage_case_memory` 永远位于 report 子图之后。顶层不读取自然语言判断是否 accepted，而是把完整
`ReportRunResult` 交给 `CaseMemoryService`：accepted 有根因时 staged/merged，accepted 无根因时
`skipped_no_root_cause`，degraded 时 `skipped_not_accepted`。最终 `DiagnosisRunResult` 还校验 ReAct
与 report 的 run/session 相同，并禁止 degraded 搭配写入成功状态。

staging 完成后，顶层会重新通过 `ReportRunResult` Schema 构造终态，并把 `stage.memory` 同步到
`AgentState.memory_candidate`；跳过时则明确清空该字段。最终结果再次要求 state 内候选与外层
`memory_stage.memory` 相同，避免未来 checkpoint、run API 和直接 workflow 调用观察到两套状态。

历史搜索错误在任何 Agent 调用前传播；ReAct、报告或 staging 的编程/数据库错误同样不吞掉。
这是有意的失败策略：无历史命中是正常空列表，依赖故障不是空列表；持久化状态未知时也不能向
调用方返回“诊断完整完成”。未来 HTTP 层应把这些异常映射为明确运行失败事件，而不是改成 200。

### 14.5 验证范围与当前限制

单元测试使用记录型协议替身验证 trigger、查询优先级/截断、同一案例跨子图复用、degraded 跳过和
搜索异常短路。PostgreSQL 集成测试运行真实顶层图、真实 ReAct/报告 LangGraph 和真实 memory
runtime：首个 session 暂存并确认，第二个 session pgvector 召回，Planner/Auditor 均收到案例，
最终 exact signature 合并且 occurrence_count 从 1 增至 2。

当前尚未提供 `/api/v1/sessions`、message/run/event 资源、LangGraph checkpoint 或顶层 runtime
lifespan factory；也未完成 similarity/common points/differences 的 Prompt/报告投影和 GraphRAG
`case`/`SIMILAR_TO` 注册。这些是后续切片，不应由当前内部工作流完成度代替。

## 15. 测试分层

- 单元测试：Pydantic 约束、Fixture、Planner/Auditor Prompt 与 Structured Output 修复、Observation、固定 capability registry、LangGraph ReAct/报告返工门禁、Provider 稳定性、向量元数据、混合评分、Evidence Bundle 预算和消融 Schema。
- 模型/MCP/编排集成测试：官方 AsyncOpenAI MockTransport、真实 stdio 握手、九工具发现、成功/失败响应、重试 trace、Planner → Action → Observation → Planner 回环，以及规则否决 → Auditor → 唯一报告返工。
- PostgreSQL 集成测试：迁移、pgvector 扩展、带 Provider 溯源的幂等种子、cosine/全文双路检索、混合评分、预算 Bundle、vector-only/vector+graph、删边消融，以及案例记忆去重/幂等/确认召回。
- Docker 验证：从镜像安装依赖，等待 PostgreSQL 健康，执行迁移/种子，再检查 API `/health`。

PostgreSQL 测试使用 `postgres` marker。普通 `pytest` 默认排除它，保持无 Docker 环境下的快速反馈；显式数据库验证使用：

```powershell
$env:DATAOPS_TEST_DATABASE_URL='postgresql+asyncpg://...'
python -m pytest -m postgres
```

## 16. 配置与生成文件说明

| 文件 | 为什么不逐行注释 | 如何理解和验证 |
|---|---|---|
| `requirements*.lock` | 由 pip-tools 机械生成，手工注释会在再生成时丢失。 | 依赖来源在 `pyproject.toml`，一致性由 `pip check` 和 Docker 构建验证。 |
| `data/fixtures/**/*.json` | 标准 JSON 不允许注释。 | Pydantic Scenario Schema 和 Fixture 测试。 |
| `data/knowledge/*.json` | 需要被标准加载器和其他语言读取。 | `KnowledgeSeedBundle`、source_span 校验和 PostgreSQL 集成测试。 |
| `data/evals/*.json` | 标准评测数据不能加入非标准注释。 | `GraphAblationCase` Schema、快速加载测试、本文档和实测报告。 |
| PNG / DOCX | 二进制格式不能可靠保存代码式注释。 | Markdown 产品基线、本文档和正式阅读版正文。 |

## 17. 当前完成度与下一步

已经完成：

- 契约与 Fixture 基线。
- 九个真实 MCP 只读 Mock 工具。
- Action → MCP → Observation 与单次瞬时错误重试。
- PostgreSQL/pgvector 图存储基础。
- 人工知识种子、可替换 Embedding Provider、真实 pgvector cosine 查询。
- 全文/向量种子合并去重、五项可解释评分和 1–2 跳显式路径扩展。
- 预算化 Evidence Bundle、稳定节点/路径引用和 vector-only/vector+graph 消融。
- 五项固定 runtime capabilities、确定性 registry、历史按需触发和健康检查契约审计。
- LangGraph capability 注入、有界 Planner Action/Observation 控制器、公开事件和真实 MCP 回环。
- Planner v2 双消息 Prompt、OpenAI-compatible Structured Outputs Provider 与一次 Schema 修复。
- 确定性报告草稿、引用/风险门禁、独立 Auditor Structured Outputs 与最多一次报告级返工。
- `case-memory:v1` 受控长期案例候选、pending/confirmed/rejected 决策、exact/pgvector 去重、同 run 幂等和 confirmed-only 搜索 API。
- `audited-diagnosis-workflow:v1` 按需召回、ReAct、Auditor 和审计后 memory staging 顶层闭环。

尚未完成：

- 模型级 Embedding Provider（当前默认实现是离线 feature hashing 基线）。
- 完整诊断 session/message/run/event 资源 API 与顶层 runtime lifespan factory。
- 会话 checkpoint、已确认案例 GraphRAG `case`/`SIMILAR_TO` 注册，以及历史共同点/差异点生成。
- 删除案例 API、28 个完整 Golden Cases 和长期记忆召回评测。
