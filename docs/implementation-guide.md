# DataOps Troubleshooter 实现原理与学习指南

本文档服务于两个目标：帮助学习者从代码理解 Agent 工程的真实边界；帮助求职者在面试中能够解释每项技术为什么存在、如何实现、如何验证以及当前仅保留的模型接入边界。

文档只描述已经进入仓库并通过验证的实现。真实模型调用、模型级 Embedding 和复杂语义比较
明确保留为接入点；其余运行时、持久化、前端和评测闭环均以代码与测试为准，避免把设计当作完成结果。

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

1. `mcp_server.server` 按配置启动独立 MCP 服务：`stdio` 子进程，或监听端口的 Streamable HTTP 网关。
2. 服务通过 MCP `list_tools` 暴露九个固定名称、输入 Schema、输出 Schema 和只读注解。
3. 客户端（`StdioMcpClient` 或 `StreamableHttpMcpClient`）建立连接并完成 MCP initialize 握手。
4. 客户端通过 `call_tool` 发送结构化参数。
5. 服务端工具调用 Fixture 仓储，返回经过 Pydantic 校验的统一响应。
6. 客户端解析 `structuredContent`，再由执行器生成 `Evidence` 和 `ToolEvent`。

这条路径确保 Fixture 只存在于 MCP 服务端，Agent 运行时看到的是标准协议 Observation。两种传输共用第 2、4、5、6 步，因此契约与传输无关——`tests/integration/test_mcp_protocol.py` 与 `tests/integration/test_mcp_streamable_http.py` 里那六条工具注解断言逐字相同，就是这条性质的可执行证明。

### 5.2 传输选型：stdio 与 Streamable HTTP

契约 ID `mcp-transport:v1`。九个工具名与 `McpToolResponse` 一个字未改，所以 `mcp-tools:v1` 保持不动：包装/传输层的变化不该假装成被包装契约的变化，这与 `model-transient-retry:v1` 单列 ID 是同一条理由。

决定 transport 的是 **client↔server 这一跳的部署关系**，不是被观测服务在哪里。stdio 的适用条件是"服务与客户端同生命周期、同主机、单客户端"：一个由 Agent 进程亲自 fork 的子进程。Streamable HTTP 的适用条件是"服务是独立部署单元、有自己的生命周期与多个客户端"。本项目的目标形态属于后者：LTS / BDS / FlashSync 由一个独立部署的 MCP 运维网关代理，网关先于 Agent 启动、独立扩缩、独立升级。

选 Streamable HTTP 作为生产路径的五条理由，全部与"被观测服务在不在云上"无关：

1. **审计记录点必须落在 Agent 信任边界之外。** 谁在什么时候取了哪条链路的证据，这份记录不能由被审计方自己保管。网关是独立进程，它的访问日志与限流计数不受 Agent 代码影响。
2. **多客户端复用同一套工具。** 人工排查、定时巡检、自愈脚本、运维大盘都要调这九个工具。stdio 下每个客户端各自 fork 一份服务并各自复制一份启动逻辑；HTTP 下它们共用一个已经在跑的网关。
3. **限流闸门要放在被观测服务侧。** 保护 LTS / BDS / FlashSync 不被取证流量打爆，是网关的职责而不是每个调用方的自觉。闸门在客户端就等于没有闸门。
4. **凭据面从三套收敛到一套。** 三个后端各自的访问凭据只存在于网关；Agent 只持有一个网关令牌。Agent 被攻破的后果因此从"三套后端凭据泄露"降级为"一个可撤销的网关令牌泄露"。
5. **长连接的资源定界有现成答案。** 连接池上限、keep-alive、超时、重试语义在 HTTP 栈里是配置项；在 stdio 下要自己写进程池化与孤儿进程回收，那是一份没人愿意维护的基础设施代码。

三条常见但**不成立**的论据，这里明确驳回，避免它们被当成本节的依据反复出现：

- **"被观测服务在云上，所以必须用 HTTP。"** 不成立。MCP 的 transport 只描述 client↔server 这一跳；server 用什么协议访问下游（HTTPS、SDK、JDBC）是它自己的实现细节。一个 stdio 的 MCP server 完全可以访问云上 API。
- **"stdio 不能长连接。"** 不成立，恰好相反：stdio 的管道天然是长连接，一个子进程可以服务成千上万次调用。真正的区别是资源定界谁来做（见理由 5）。
- **"流式返回是 HTTP 独有的。"** 不成立。MCP 的进度通知与分块结果在 stdio 上同样工作，它们是协议层能力而不是传输层能力。

**为什么共享连接池但不复用 MCP 会话。** 客户端持有一个长寿命 `httpx.AsyncClient`（省掉每次调用的 TCP/TLS 握手，也彻底省掉 stdio 的解释器启动开销），但**每次调用新建一个 MCP 会话**。长期持有一个 `ClientSession` 才是"HTTP 长连接"的极致形态，代价却是把 anyio task group 与 cancel scope 的生命周期挂在跨任务边界上：`call_tool` 在 Worker 任务里被 `asyncio.timeout` 取消时，取消会穿进这套内部结构，而 enter/exit 跨任务的 cancel scope 是 MCP SDK 上已知的踩坑区。现在的取舍拿到了绝大部分收益，同时保住"每次调用完全隔离"这个性质——正因为如此，`execute_tools` 的 1–3 个并行 Action 不需要任何额外并发推理。

池化不需要任何 hack：`streamable_http_client(url, http_client=...)` 只在自己创建 client 时才 `enter_async_context`（`mcp/client/streamable_http.py:637-654`），调用方提供的 client 不会被 SDK 关闭。

**网关跑 `stateless_http=True`。** 每个请求一个全新 transport，服务端没有会话表、没有 DELETE、不需要粘性路由，因此网关可以随时重启或水平扩容而不丢状态。客户端仍保留 `terminate_on_close=True` 作为安全网：当前不会真的发出 DELETE（无 session id），但一旦有人把网关改成有状态，这个默认值就是防止服务端会话泄漏的那道保险。

**DNS rebinding 防护必须显式关闭，"不传参"不等于"关闭"。** `TransportSecuritySettings` 这个类的默认值是开启，但真正的陷阱在 `FastMCP.__init__`（`mcp/server/fastmcp/server.py:178-183`）：当 `host` 属于 `127.0.0.1` / `localhost` / `::1`——而 FastMCP 的 `host` 默认值恰好就是 `127.0.0.1`——它会**自动**替你构造一份开启防护的设置，并把 `allowed_hosts` 限死为三个回环形式。于是容器里按 service 名访问的请求（`Host: mcp-gateway:8900`）会得到 HTTP 421 Misdirected Request，api 的 lifespan 在启动期工具发现处失败、进程以退出码 3 结束。这个故障在 `docker compose up` 第一次真跑起来时才出现：集成测试全部通过 `127.0.0.1` 连接，正好落在允许列表里；网关 healthcheck 的**第一版**也看不到它，因为那一版只断言匿名 GET 得到 401，而 401 在鉴权中间件就短路了，根本走不到 MCP 应用——这条经验现在固化成 `mcp_server/healthcheck.py` 的第二段断言（带令牌、`Host` 写成 service 名的 `initialize` 必须回出 `protocolVersion`）。关闭防护的理由是它在本部署里保护不到任何东西：网关不发布宿主端口、不对浏览器开放，真正的门禁是 Bearer 令牌，而被 rebinding 诱骗的浏览器拿不到令牌，请求会先变成 401；反过来维护一份 `allowed_hosts` 等于把部署地址抄第二遍，换 service 名、加 ingress 或上 k8s 时必然漂移。`tests/integration/test_mcp_streamable_http.py` 里有一条用裸 httpx 覆写 `Host` 头的回归用例，把这条只在容器网络里出现的失败面钉在了不需要容器的测试里。

**`trust_env=False` 与 `follow_redirects=False` 是安全要求，不是性能调优。** httpx 默认会读环境变量与 Windows 注册表里的系统代理，那会把网关 Bearer 令牌经一个不在信任边界内的代理转发；同时它会把"端口无人监听"变成代理侧读超时，让 `SERVICE_UNAVAILABLE` 被误分类成 `TIMEOUT`（后者与前者都可重试，但错误归因会把网关宕机读成网络抖动）。关掉重定向是同一条理由的另一半：网关地址是显式配置的内网地址，任何重定向都意味着配置错了，跟随它等于把令牌送去一个没打算信任的主机。

**鉴权 fail-closed，且复用资源 API 的守卫。** `mcp_server/security.py` 不实现任何鉴权算法，而是复用 `app/api/security.py` 的 `ApiSecurityGuard`：同一套 SHA-256 摘要 + `hmac.compare_digest` 定长比较、同一套先限流后鉴权的顺序、逐字相同的 401 响应体。两处各写一份的后果是可预期的——其中一份会先长出"调试用后门"。`streamable-http` 缺令牌拒绝启动，`stdio` 却配了令牌同样拒绝启动（两个方向都拦，因为后者说明部署者以为自己在跑网关）。401/403 映射为 `PERMISSION_DENIED` 而不是 `SERVICE_UNAVAILABLE`：后者在 `RETRYABLE_TOOL_ERRORS` 内，会让一个配错的令牌把每次调用变成两次网关请求。

网关限流有自己的配额（默认 600/60s），不与资源 API 的 120/60s 共用：一次 `call_tool` 在无状态模式下是三个 POST，而且全部来自同一个 api 容器地址，所以按来源计的窗口实际上是网关的全局闸门。

**stdio 保留，但不再演进。** 它仍是仓库默认值（`DATAOPS_MCP_TRANSPORT=stdio`），因为测试与离线评测必须能在没有网关的机器上跑通，而 `tests/conftest.py` 会清空所有 `DATAOPS_*`。生产形态由 `compose.yaml` 显式声明成 `streamable-http`，而不是靠翻仓库默认值。从这一节起，stdio 的定位是**可选配置与代码学习路线**：不再为它新增功能，也不再为它新增测试；已有的 stdio 集成测试保留，因为它们同时充当"契约与传输无关"的对照组。

`/health` 的 `mcp` 小节逐字公开 `transport`、`contract_id`、`auth_required`、工具超时与重试预算，但不公开 URL 里的任何凭据或令牌摘要。这个小节存在的唯一原因是：**"以为在打网关、其实还在起 stdio 子进程"是一种不会报错的部署漂移**，只有把实际传输暴露出来才能在不读代码的情况下发现它。同理，compose 里网关的 healthcheck（`python -m mcp_server.healthcheck mcp-gateway:8900`，见 `mcp_server/healthcheck.py`）断言的是两段而不是"端口能连上"：匿名 GET 必须被挡成 401（证明鉴权中间件真的插在应用前面），且带令牌、`Host` 写成部署 service 名的 `initialize` 必须打到 MCP 应用并回出 `protocolVersion`（证明请求真的穿过了中间件、传输安全策略没把这个主机名挡掉）。探针的强度就是部署门禁的强度——`api` 的 `depends_on` 挂在这个判定上，探针断言不到的东西就是 `compose up` 抓不住的东西。令牌只从容器环境读、不进 argv、不出现在任何 reason 字符串里，因为 `docker inspect` 会公开 healthcheck 命令并留存 `Health.Log`；`tests/integration/test_mcp_streamable_http.py` 有专门用例断言这一点，以及错令牌、缺令牌、端口无人监听三种失败各自的判定。

### 5.3 只读与安全属性

每个工具都声明：

- `readOnlyHint=true`
- `destructiveHint=false`
- `idempotentHint=true`
- `openWorldHint=false`

协议集成测试读取这些注解，防止以后新增工具时意外变成写操作。项目不会实现自动重跑、删表、扩容或修改同步配置。

### 5.4 重试原理

执行器只对 `TIMEOUT` 和 `SERVICE_UNAVAILABLE` 重试一次。空结果、权限拒绝和非法请求继续重试不会增加信息，因此直接返回。

每次尝试都生成独立 `ToolEvent`，事件 ID 包含 trace、工具名、规范化请求和 attempt 的稳定摘要。即使第二次成功，也保留第一次失败，便于观察延迟、失败率和真实调查过程；同一工具查询不同资源或时间窗时也不会发生 ID 冲突。

传输换成 HTTP 之后，这套预算一个字未改，只是新增的失败面必须被正确归类才能落进同一套语义：连接被拒、5xx 与其他传输异常映射为 `SERVICE_UNAVAILABLE`（网关重启属于瞬时故障，值得重试一次），401/403 映射为 `PERMISSION_DENIED`（令牌配错不会因为重试而变对）。分类顺序上 HTTP 状态码优先于超时判断：网关在 401 之后关闭连接可能顺带产生一次读超时，若先看超时就会把"令牌错"误判成瞬时故障。执行器与 `McpToolExecutor` 的重试代码对两种传输完全共用，客户端只按 `McpToolClient` Protocol 注入。

## 6. FastAPI lifespan 与健康检查

FastAPI lifespan 在开始接收请求前执行以下检查：

1. 加载并校验全部 Fixture。
2. 加载 Golden Case，并确认它引用的场景真实存在。
3. 检查版本化 Planner Prompt 是否存在且 ID 匹配。
4. 通过真实 MCP 协议发现九个固定工具。
5. 配置数据库时，建立 PostgreSQL 连接，读取知识节点/边数量，并确认全部节点已位于当前 Provider/维度空间。

任何强依赖不满足时，应用启动失败，而不是在用户提交诊断后才暴露配置错误。未配置数据库的纯单元测试模式会明确返回 `database_status=disabled`；Docker 演示模式必须返回 `database_status=ok`、`knowledge_nodes_embedded=40`，并公开不含凭据的 Provider、维度和评分权重快照。

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

生产默认 `bge-m3:v1` 由 `OpenAICompatibleEmbeddingProvider` 通过 OpenAI-compatible `/embeddings` 调用硅基流动托管的 `BAAI/bge-m3`（1024 维，多语言）。它原生处理中英混排的运维术语，因此“任务卡住”与“作业长时间无响应”能得到真实语义相似度，这是 feature hashing 基线做不到的。实现关闭 SDK 隐式重试（`max_retries=0`），使失败次数等于真实请求次数以便成本核算；按 `embedding_batch_size` 分批请求，并严格校验返回条数、按 `index` 重排、逐项检查维度/有限性/非零，任一项不符即抛 `EmbeddingProviderError` 让整批失败，绝不写入半个向量空间。

两个 Provider 通过版本化 ID 严格隔离，128 维旧行与 1024 维新行即使同库共存也不会进入同一次 cosine 排序，因此切换模型不需要迁移列类型，只需重跑种子。凭据只从 `DATAOPS_EMBEDDING_API_KEY` 读取并以 `SecretStr` 持有；`Settings` 启动校验要求非确定性 Provider 必须有 key，且 `bge-m3:v1` 的维度必须等于 1024，避免半配置部署一边宣称语义检索、一边继续输出 feature-hash 向量。

## 8. 显式 GraphRAG 路径

### 8.1 为什么关系必须进入边表

如果把“LTS 依赖 BDS，BDS 依赖 FlashSync”只写在一段文本里，检索系统只能返回相似文档，无法可靠证明链路节点和边是否完整。显式边表允许系统返回：

```text
component_lts
  -[DEPENDS_ON]-> component_bds
  -[DEPENDS_ON]-> component_flashsync
```

每条边保存来源 ID 和原文跨度，最终报告可以引用 `path_id` 并回溯到人工知识种子。

`graph-seed:v12` 在 v1–v10 的 47 节点/61 边基础上，增加订单履约链 LTS/BDS/FlashSync 三个任务、
订单事件数据集、增量窗口静默漏数症状、水位线时区错配根因和受控回补方案共七个节点；同时增加
三个 RUNS_ON、两个 DEPENDS_ON、PRODUCES、CONSUMES、MANIFESTS_AS、CAUSED_BY 和 RESOLVED_BY
共十条边，当前合计 54 节点/71 边。旧节点和边继续保留各自 v1–v10 source；只有订单履约链与水位线
知识使用 `synthetic_cross_chain_knowledge_v11`。v12 不改变拓扑，只为八个 `solution` 节点补上
`remediation_risk_level` 声明（三个 high、五个 medium），把"这个方案有多危险"从代码常量变成人工
知识。高风险方案只描述冻结位点、人工校准、幂等验证、
小批量回补和回滚点，不授予自动修改水位线或生产写入权限。任务依赖表达传播，数据边表达实际交付，症状→根因→方案边
表达可复用解释，三类关系不能互相替代。
Bundle 版本描述组合快照，逐项 source_id 描述原始出处，两者分离可避免升级种子时伪造旧知识历史。

#### 8.1.1 处置风险等级由知识声明，而不是由报告层猜

`remediation_risk_level` 只能出现在 `solution` / `sop` 节点上，且这类节点必须声明它——"当且仅当"
是一条双向不变量，由 `app/retrieval/models.py::validate_remediation_risk_declaration` 在
`KnowledgeNode` 与 `BundledKnowledgeNode` 两处共用，并由 `knowledge_nodes` 表的
`ck_knowledge_nodes_remediation_risk_level` CheckConstraint 在绕过 Pydantic 直接写库时兜住。
反向约束同样重要：如果 `symptom` / `component` 这类事实节点也能声明风险，任何一条被召回的事实
证据都能抬高整份报告的风险等级，而它根本不描述"要对生产做什么"。

刻意没有默认值。`app/reporting/draft.py::_build_remediation_steps` 之前把所有知识方案固定成
`RiskLevel.MEDIUM`，后果不是"偏保守"而是 `RiskLevel.HIGH` 在生产路径上不可达：
`derive_report_risks` 的高风险分支成了死代码，Golden 案例声明的 high 期望永远不可能命中，
`risk_level_hit_rate` 的上限因此被实现而不是被模型能力锁在 0.667。现在缺声明会在 Bundle 边界
直接 `ValidationError`，而不是静默退回 medium——静默降级正是这次要消除的东西。迁移
`20260716_0010` 先回填既有 solution/sop 行为 `medium` 再建约束（顺序反了会让存量行违约），
因此它也不是一个带默认值的 NOT NULL 列。

文档切片仍固定 medium：切片是原文摘录，没有声明字段可读，而 `draft.py` 明确不允许从动作文本
猜风险（关键词改写就能改变控制语义）。一个案例的实测风险等级是被召回方案节点的最大值，因此它
仍然依赖检索选择——实现约束解除不等于指标自动达标。

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

`GraphRetrievalResult` 是完整检索结果，不应原样注入 Planner。`app/retrieval/budget.py` 将它转换为 `graphrag-evidence-bundle:v3`：先按最终检索排序尝试加入路径及其全部节点，再补充未出现的高分种子，最后加入文档切片。路径是原子候选，任何字节、节点或路径预算不满足时整条省略，不会切断边或截短正文。

默认预算为 6000 个 UTF-8 JSON 字节、8 个唯一节点、4 条路径和 3 个文档切片。使用字节而不是某个供应商 tokenizer，能在尚未绑定模型时保持精确可重放；未来模型适配层仍可在此硬上限内增加供应商 token 检查。`used_bytes` 只统计规范序列化后的 selected_nodes/selected_paths/selected_documents 主体，omitted IDs 属于诊断元数据。

文档切片单独设数量上限而不是与节点共享，是因为切片正文远长于知识节点：共享上限会让几段 Runbook 正文挤掉全部图证据，报告随之退化成"引用了文档但说不出故障如何传播"。装入顺序同理把图证据放在文档之前，关系路径是本系统区别于普通 RAG 的解释能力。

知识节点使用 `kn_<node_id>` 作为稳定证据引用，路径继续使用数据库边序列生成的 `path_id`，文档切片使用 `dc_<hash>`。Bundle 不包含 embedding、模型原始推理或数据库内部状态；`truncated=true` 明确表示仍有候选因预算未注入。

### 8.7 Vector-only / Vector+Graph 消融

检索服务提供三个显式模式：`vector_only` 只返回向量种子，`vector_graph` 在相同向量种子上扩图，`hybrid_graph` 再加入全文通道并作为生产默认值。模式进入 `graphrag-retrieval:v3` 输出，避免通过隐藏布尔开关运行无法复现的实验。

`data/evals/graphrag_ablation_cases.json` 使用稳定知识节点 ID 标注预期根因和必要有序路径。`app/retrieval/ablation.py` 计算根因节点是否可见，以及必要节点序列在最佳真实路径中的覆盖比例；它不调用 LLM，因此当前指标只描述检索层，不冒充最终报告准确率。

首个案例实测值记录在 `docs/graphrag-ablation-results.md`。结果显示根因在 vector-only 已经命中，图扩展没有虚报额外根因收益；图的可解释增益体现在必要因果链完整率从 0.0 变为 1.0。

### 8.8 两阶段检索与 cross-encoder 重排

双塔 embedding 为了可索引必须独立编码查询和文档，因此无法建模两者的交互；cross-encoder 把查询与候选拼在一起联合打分，在小候选集上判别力显著更强但无法预先建索引。主流做法因此是两阶段：一阶段多召回、二阶段精排少量候选。

`app/retrieval/reranker.py` 定义 `RerankerProvider` 协议（`provider_id`、`model`、顺序对齐的 `rerank`），`HttpCrossEncoderReranker` 通过 Jina/Cohere 风格 `/rerank` 端点调用硅基流动托管的 `BAAI/bge-reranker-v2-m3`。该端点不属于 OpenAI 规范，所以这里直接用 httpx 而非 OpenAI SDK。**关键实现细节：`/rerank` 按 `relevance_score` 降序返回结果并携带 `index` 字段，必须按 `index` 回填到输入位置**；按响应顺序读取不会报错，只会把最高分错配给第一个候选，这是此类集成最容易出现且最难发现的缺陷，`tests/unit/test_reranker.py` 用乱序响应专门锁定这一点。

检索服务的两阶段流程是：`_candidate_limit` 把 `seed_limit` 乘以 `rerank_candidate_multiplier`（默认 3）作为一阶段召回数，并同时受仓储 top-k 契约（20）和端点单次文档上限（64）双重设限——多召回是二阶段收益的唯一来源，候选太少精排就无从改名次；随后融合分数并截断到 `seed_limit`，只有存活的种子参与图扩展。融合公式是 `final_score = (1 - rerank_blend_weight) * hybrid_score + rerank_blend_weight * rerank_score`，默认权重 0.4。选择线性融合而不是直接用重排分覆盖，是因为 cross-encoder 只看查询与文本的语义匹配，完全不知道节点可靠性、图路径强度与命中通道；两者相加才能既吸收精排判别力，又保留知识库自身的可信度信息。

三层分数分开保存并进入 `graphrag-retrieval:v3` 契约：`hybrid_score` 是一阶段五项加权分，`rerank_score` 是二阶段分数，`final_score` 是显式融合结果。领域模型强制一条不变量——`rerank_score` 为空时 `final_score` 必须等于 `hybrid_score`，因此“重排了顺序却没有重排分数”在结构上无法构造，评测总能把名次变化归因到召回或精排中确定的一个环节。`candidate_count` 记录精排前的候选规模，使“重排带来多少提升”有可核对的分母。路径不单独送进 cross-encoder：路径的相关性来源是“这个种子值得展开”，对拼接文本重复打分只增加成本不增加信息，因此路径按同一权重继承种子的 `rerank_score`。

重排是可选增强而不是可用性依赖。`rerank_provider=disabled` 时工厂返回 `None`（而不是一个恒等替身），检索结果里的 `reranker_model` 因此真实为空；运行时 `RerankerError` 或分数长度不齐一律降级为保留一阶段排序，并同时把 `reranker_model` 置空、`rerank_blend_weight` 归零。这三个字段一起构成诚实性保证：任何报告都无法把未精排的顺序说成精排结果。证据预算按 `final_score` 选择注入上下文的节点与路径，所以重排真正影响“哪些证据进入 Prompt”，而不只是展示顺序。

### 8.9 文档 RAG：第二条知识通道

知识图能回答“故障如何沿依赖传播”，但排障的最后一步需要的是可执行处置步骤，而这些步骤只写在 Runbook、SOP、复盘和 FAQ 里。因此系统有第二条平行通道：`app/retrieval/documents.py` 定义领域契约，`document-retrieval:v1` 是它的对外结构。两条通道共享同一个 PostgreSQL、同一个 Embedding Provider、同一个 cross-encoder 和同一份评分诚实性不变量（`app/retrieval/scoring.py`），但保持独立表与独立仓储：切片是“一段可执行步骤”，节点是“一个实体”，混在一张表里会让向量空间过滤和评分因子互相污染。

**切片而不是文档是检索与引用单元。** `app/retrieval/chunking.py` 按 Markdown 标题层级确定性切分，每个切片保留 `heading_path`（“文档标题 > 章节 > 小节”）和文档内连续 `ordinal`。`dc_<hash>` 由 `doc_id|ordinal` 的 SHA-256 前 16 位生成，因此同一文档重新导入后引用不变，历史报告里的脚注仍指向同一段正文。标题路径必须参与 embedding 编码：SOP 的关键词经常只出现在小节标题上（“限流阈值调整”），只编码正文会让这类片段在语义通道彻底消失。切片正文上限 1200 字符，这个数字不是随手取的——它必须小于 `RemediationStep.action` 的 2000 字符上限，否则一个超长切片被提升为处置建议时会在报告构建阶段抛 ValidationError。

**三因子评分，刻意不复用图侧的五因子。** 默认权重是 semantic 0.60 / lexical 0.25 / authority 0.15，其中 authority 直接取文档人工声明的 `reliability`。`path` 对没有关系边的切片没有意义，`freshness` 也无法从静态语料得到诚实取值，硬凑五项只会让公式看起来更复杂而不更准确。权重和不为一时在 Settings 初始化阶段失败，不做隐式归一化：一个错配权重被静默修正后，评测报告的分数区间仍看起来正常，却已无法与文档基线比较。

**导入采取“先删该文档全部切片再整批插入”。** 重新切片会改变切片数量与边界，逐条 upsert 会让旧版本的尾部切片以过时正文残留在库里继续被召回，而这种污染在检索结果上完全看不出来。知识图与文档语料在 `app/persistence/seed.py` 的同一个事务里提交，避免出现“图已就绪但文档缺失”的中间状态——那种状态下检索仍会返回结果，只是永远少了一条通道。启动审计对切片执行与知识节点相同的全有或全无向量检查：`document_chunks_embedded != document_chunks_loaded` 直接拒绝启动，因为部分切片缺向量时语义通道只会少召回而不报错。

**只有 Runbook/SOP 的步骤小节能变成处置建议。** `app/reporting/draft.py` 的 `_is_remediation_chunk` 匹配标题路径最后一段是否含“处置步骤/确认步骤/恢复步骤”，且文档类型属于 Runbook/SOP。复盘的“改进项”是长期治理动作而非本次处置，FAQ 是判断依据而非操作，Runbook 里同样存在“禁止操作”“升级条件”这类正文——把它们当成待执行动作，报告就会建议运维去做一件文档明确禁止的事。判定依据是作者显式声明的标题，而不是正文关键词或模型判断，因此 Golden 回放能稳定复现同一份建议。文档来源的建议一律标记 medium 风险，前置条件包含“确认该修订版本仍适用于本次故障范围”，并且永不声称系统已执行。

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
来源供后续事件审计。当前运行时已经接通 PostgreSQL/pgvector 长期记忆召回；模型级复杂语义比较仍
保留为接入点，不能把确定性检索结果包装成真实模型质量。

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
       -> execute_tools
       -> planner_react
       -> END
```

`select_capabilities` 调用固定 registry，并把意图和名称注入 AgentState；`planner_react` 调用
可替换 `PlannerAgent` 协议，接收结构化 PlannerDecision；`execute_tools` 使用注入执行器跨真实
MCP 执行本轮整批 Action，随后原子回写 Evidence、ToolEvent、observation_refs 和 react_step。
编译后的图名为 `dataops_bounded_react_v3`，契约 ID 为 `langgraph-react-loop:v3`。

LangGraph 的 state_schema 使用 `ReactGraphState` Pydantic 模型。每个节点接收和返回强类型
模型，框架最终给出的映射也立即通过 `model_validate` 重建。Planner、Executor、Registry 和
截止时间通过 `context_schema=ReactGraphRuntime` 注入，不进入 checkpoint，也不会与并发运行
共享可变状态。

### 10.2 Planner 协议为何不是占位 Agent

`app/agents/planner.py` 的 `PlannerAgent` 是依赖反转边界：生产实现必须接收
`PlannerTurnContext` 并返回 `PlannerDecision`。它不提供工具执行或 Observation 写入方法，因而
模型供应商适配器不能绕过图节点。OpenAI-compatible Chat Provider、v5 Prompt Renderer 和一次
结构化输出修复现已实现；报告草稿和独立 Auditor 由 `audited-report-workflow:v2` 接续。
Scripted Planner 测试仍用于隔离纯图控制流，官方 SDK MockTransport 测试则验证真实模型协议边界。

PlannerTurnContext 会再次检查 AgentState.intent、active_capabilities 和 CapabilitySelection
一致，并拒绝预算耗尽后的模型调用。remaining_time_ms 来自控制器的单调时钟截止时间，模型只能
看到剩余预算，不能自行延长。

### 10.3 Action 门禁与重复检测

PlannerDecision 通过 Pydantic 只证明 JSON 结构合法，仍不足以安全执行。控制器在 MCP 前按固定顺序
检查：引用是否存在、批次是否超过配置并行上限、批次是否超过剩余步数预算，然后逐个 Action 检查工具
是否属于当前组件范围、trace_id 是否等于 run_id、Action 是否已经执行。任何失败都生成 `policy_blocked`
事件和公开 stop_reason，不调用 Executor。

门禁一律整批拒绝，不做截断执行。截断看起来更"宽容"，但会让 Planner 下一轮基于"我提交的三个调用
都发生了"的错误前提推理，排查成本远高于直接停止。

重复检测先从 ToolAction 排除每轮必变但不影响查询语义的 trace_id，再把工具、资源、时间窗和场景
规范化为排序键、紧凑 UTF-8 JSON 并计算 SHA-256。同工具不同资源仍允许；同一查询即使恢复后使用
新 run trace 也会拦截。指纹集合同时覆盖同批次内部与此前轮次（含 checkpoint 恢复后的历史），因为
一批里放两个完全同参的调用只会浪费两个步数换同一个 Observation。trace_id 仍由独立策略门禁强制等于
当前 run_id，不能借去重逻辑绕过审计。

MCP 内部 TIMEOUT/SERVICE_UNAVAILABLE 重试仍由 McpToolExecutor 负责。一个 Planner Action
无论产生一个还是两个 ToolEvent，react_step 都只增加一；这样“最多 10 步”表达调查决策预算，
不会被传输重试歪曲，同时总网络尝试仍完整可审计。

### 10.4 有界并行工具调用

"查 LTS 状态 + 查同一任务日志 + 查依赖拓扑"本来就是三个互不依赖的只读操作，串行执行白等三倍
延迟，而产品的 P95 目标是 ≤30 秒。因此 `execute_tools` 接受一批 1 到 `max_parallel_tool_actions`
（默认 3，硬上限 `MAX_PARALLEL_TOOL_ACTIONS = 3`）个 Action，用
`asyncio.gather(..., return_exceptions=True)` 并发执行，汇总后把任一 `BaseException` 原样重抛，再用
`zip(actions, observations, strict=True)` 按 Planner 给出的顺序确定性合并状态。并发因此只影响等待
时间，不影响状态写回顺序，也不会让某个失败被 gather 静默吞掉。

并发安全来自两处既有设计而不是新增锁：`StdioMcpClient` 每次 `call_tool` 都新起子进程、`initialize`
再拆除，所以一批 Action 跑在彼此隔离的 MCP 会话上；`RunTraceCollector` 在 span 开始时就预留序号、
结束时按序号回填，并让每个 `asyncio.gather` 任务复制一份 `_CURRENT_PARENT` ContextVar，所以
`run-trace:v1` 的"序号连续"和"父先于子"两条不变量在并发下仍然成立。遥测形状是一个
`react.tool_batch` 父 span（属性含 `action_count`、`tool_names`、`react_step`、`ok_count`、
`failed_count`）包住每个 Action 的 `react.tool_call` 子 span；只要有一个 Action 失败，父 span 标记
ERROR。

预算语义是这里最关键的产品决定：`react_step += len(actions)`。并行只压缩等待时间，不发放额外取证
预算，所以把并行度从 1 调到 3 会降低 P95，但不会让模型多看证据，也不需要同步调大
`max_react_steps`。`app/core/settings.py` 因此额外拒绝 `max_parallel_tool_actions > max_react_steps`
的配置：那种组合下控制器每轮都要把批次上限压回剩余步数，配置写的值永远不生效，宁可启动失败也
不要留下"看起来配了 3 实际只能 1"的误导。Prompt 与门禁同源同样由控制器保证——`planner_react` 注入
`max_parallel_actions=min(config.max_parallel_actions, remaining_tool_calls)`，单元测试断言这个序列
在 6 步预算下从第 4 步开始收敛为 `[2, 1]`。

对外契约刻意保持向后兼容：`ReactPublicEvent.tool_name` 仍是单值（批次大小 > 1 时为 null），新增
`parallel_action_count` 说明本轮批量；`run_events` 里两者一起落库，因为回放时间线时"三步串行"和
"一批三个并行"看起来完全一样，而这正是解释延迟改善的唯一依据。批内每个 Action 各产生一条
OBSERVATION_RECORDED 事件，单个工具失败依旧单独可见，所以 `diagnosis-resources:v4` 和 `/demo`
前端都不需要改动。

### 10.5 总超时与最后完整状态

`DATAOPS_REACT_TOTAL_TIMEOUT_SECONDS` 默认 240 秒，覆盖 LangGraph 调度、Planner 等待和 MCP
执行，独立于单工具 timeout。控制器使用 `asyncio.timeout` 取消超时节点，并以 LangGraph
`astream(stream_mode="values")` 持续保存最后一个完整 Pydantic 状态。这样第二次工具卡住时，
第一次已经取得的证据不会因为总超时丢失；终态追加 `total_timeout`，但不会伪造失败节点结果。

步数预算与墙钟预算的默认值都由真实模型评测校正过，这里记录完整校正链条，因为这些数字看起来
只是"调大一点"，实际每次各修掉一类结构性错误。**步数 6 → 8**：Golden 集里跨组件案例的
`required_tools` 最多 6 个，6 步预算等于零余量，而真实模型总要花一到两步试探，于是每次试探都
直接换掉一个必需取证——实测跨组件案例执行满 6 步后以 `react_budget_exhausted` 结束，却漏掉
`bds.get_table_info`，满覆盖在这种配置下是不可达的。**步数 8 → 10**：8 步修掉了覆盖率下界，却
没有留出"收口回合"。循环在 `react_step >= max_steps` 时先停机再判断，所以恰好把预算用满的
Planner 永远拿不到最后那次决策机会：证据其实已经齐了，run 却只能以 `react_budget_exhausted`
结束，报告基于"调查未完成"起草，Auditor 判 `report_incomplete`，唯一一次返工预算用尽后转
`safe_degraded`，最终 `root_causes` 为 0。这不是模型不收口，而是预算算术：一次真实 run 用
3+3+2 的批次序列刚好填满 8 步，而最后那两个 Action 取的正是 Golden 要求的必需证据，因此"让
Prompt 更早收口"只会用证据覆盖率换一个好看的 `stop_reason`，方向是反的。10 步在同一条轨迹上
留出一个整批余量。要**结构性**地消除这条失败面，得给"只允许 finish 的最后一回合"单列保留额度，
那会改动 `langgraph-react-loop` 的循环语义，属于另一个切片；在此之前它是一个已知的、可通过配置
缓解但未被证明不可能命中的边界。**墙钟 60 → 240 秒**是步数调整的必然后果：实测 Planner 单次
决策 8–18 秒，10 步在最坏情况下要五次决策再加工具与检索时间（一次 3 批次的真实 run 端到端
96.7 秒），仍用 60 秒只会把"时间不够"伪装成"步数用完"，而这两种终止原因对使用者的含义完全不同
（前者要加时间或减并发，后者要重新设计取证顺序）；240 秒还必须容得下一次瞬时重试的最坏开销。

递归上限按 `max_steps * 2 + 6` 设置，覆盖路由、每次 execute/planner 回边和最终预算检查。
它是框架死循环的第二道防线，业务停止仍由 react_step 和 stop_reason 决定。

### 10.6 公开事件与审计 ID

`ReactPublicEvent` 只记录稳定 ID、序号、类型、公开摘要、工具名、批次大小、Observation 引用和停止
原因，不包含 Thought。终止类事件强制 stop_reason；`ReactRunResult` 强制最终 AgentState 和最后事件都
处于可解释终态，防止条件边错误导致无声结束。

Evidence 与 ToolEvent 的稳定 ID 现在包含规范化请求身份。此前同一 trace 内同一工具查询不同
资源可能共享事件 ID；加入资源、时间窗、场景和 trace 后，不同参数调用可独立寻址，而完全相同
请求重放仍稳定。合并状态时若同 ID 的结构化载荷不同，控制器显式失败，不覆盖旧审计事实。

### 10.7 验证范围

单元测试覆盖 capability 注入、Action/Observation 回写、同参拦截、组件越界、trace 漂移、
无效证据引用、步数耗尽、总超时和不同参数审计 ID；并行部分额外用一个记录 `max_in_flight` 的探针
执行器证明一批三个 Action 真的同时在飞（而不是被顺序 await），并断言它消耗三个步数、批内同参调用
在任何执行前被拦截、超并行上限与超剩余预算分别停在
`parallel_limit_exceeded` / `parallel_budget_exceeded`。集成测试用 Scripted Planner 发出
`lts.get_task_status`，Action 必须经过 LangGraph 和真实 stdio MCP，再由第二轮 Planner 读取
回写证据并 finish；另一个集成测试让真实 SDK 适配器一次提交两个 LTS Action，跨两个真实 stdio 子进程
并发执行后回到模型第二轮，这是并行批次唯一跨真实子进程的证明。这些测试证明控制器闭环真实存在，
但不宣称付费模型推理质量。

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

### 11.2 v8 Prompt 的 system/user 隔离、追问上下文、历史解释、并行批次与门禁前提

`planner-react:v8` 使用两个独立文件：system 只包含角色、安全和输出行为；user 承载查询、同会话
上一轮公开报告、raw confirmed 案例、确定性 history_case_matches、工具 Evidence、GraphRAG 和预算。
历史相似度/差异同样是不可信低优先级 JSON，不能覆盖 system 的实时事实优先规则。

v8 相对 v7 只补齐三处"控制器已经在执行、但模型看不到"的口径，不改变消息角色边界：可引用 ID 白名单
改由 `collect_reference_sources` 与报告层同源生成并公开每个 ID 的 `source`（此前 Planner 侧更窄，
模型引用 Bundle 知识证据反而被整批拒绝）；system 侧写明只有 `source` 为 `tool` 的实时 Observation
引用能把假设升为 `supported`；user 侧新增 `{unexecuted_priority_tools}`，并规定该列表非空且工具
可能改变结论时不得直接 `evidence_sufficient`。

v5 相对 v4 改两件事：`call_tool` 从"选一个 Action"变成"提交 1 到 `max_parallel_actions` 个互不
依赖的 Action"，并新增 `remaining_tool_calls` 与 `max_parallel_actions` 两个运行预算占位符。这两个
数由渲染层直接算出来交给模型，而不是让模型自己做减法：一批 N 个 Action 会消耗 N 个步数，模型算错
就会提交刚好超预算的批次，而每次被控制器整批拒绝都白花一次模型调用。system 侧对应新增一段批次
硬约束，明确"并行只缩短等待时间，不增加取证预算"，以阻止模型用广撒网代替假设驱动。

v6 把同一条原则补齐到剩下两个门禁输入 `trace_id` 与 `citable_refs`。这次升版由真实模型实测直接
逼出来：`live-golden-eval:v1` 首次带真实 `gpt-5.6-sol` 跑三条 Golden 案例时，`executed_tools` 全为
空，两例停在 `invalid_evidence_reference`、一例停在 `trace_id_mismatch`——一个工具都没执行。原因
不是模型能力问题，而是 v5 Prompt 缺少判定输入：`react_loop` 要求每个 Action 的
`arguments.trace_id` 逐字等于当前 `run_id`，可 `run_id` 只存在于 `ReactGraphState` 里；
`evidence_refs` 只接受 `state.evidence` 的 `evidence_id` 与 `state.retrieved_paths` 的 `path_id`，
而 Evidence Bundle 里最显眼的标识恰好是不可引用的 `node_id`。确定性脚本替身永远能直接从状态里取
run_id、也不会去引用 node_id，所以 390 个离线用例全绿也照不出这个缺口——这正是必须有一次真实模型
冒烟评测的原因。v6 因此显式渲染 `{trace_id}` 与 `{citable_refs}`（首轮为空数组），并在 system 侧
说明三条：trace_id 逐字复制、evidence_refs 只取白名单、Bundle 的 node_id/文档标识不是 evidence_id。

v7 由同一次真实模型评测的第二轮结果逼出来：工具执行恢复正常之后，三条案例的
`root_cause_top1_hit_rate` 与 `stop_reason_hit_rate` 仍然实测为 0，`accepted_report_rate` 只有
0.333。原因同样不是模型能力，而是两个契约缺口。第一个是**结论没有入口**：报告根因由
`AgentState.hypotheses` 确定性投影而成，而写入它的唯一通道是 `PlannerDecision.hypothesis_updates`；
v6 的 `HypothesisUpdate` 只有 `hypothesis_id` / `status` / `evidence_refs`，没有任何字段能承载新假设
的症状与候选根因，`react_loop` 也从未把更新投影回状态。实测运行里模型在 `decision_summary` 写出了
正确根因（“partition_date 参数值 20260713 不符合 yyyy-MM-dd 格式”）并提交了 `status="new"` 的更新，
却因为契约无处安放内容而被整体丢弃，于是 `root_causes` 恒为空，Auditor 以 `report_incomplete` 指向
`$.root_causes` 否决，返工删得更空，第二轮必然 `safe_degraded`。第二个是**停止原因是自由文本**：
`stop_reason` 原为 `str`，模型给出的是整段中文理由，既让公开事件带上近似结论叙述，又让 Golden 的
七个分类期望永远无法命中。v7 因此把 `stop_reason` 收成 `PlannerStopReason` 枚举、给
`HypothesisUpdate` 加上 `symptom` / `candidate_root_cause`（`status="new"` 时必填），并在 system 侧
明确写出“hypothesis_updates 是结论进入报告的唯一通道，decision_summary 不会被解析”。假设的组件
范围与置信度都不由模型自述：组件取本次已批准的 capability 组件，置信度由状态确定性映射，
因此报告里不会出现无法复算的模型自评数字。

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

所有出站客户端共用 `app/core/http_identity.py` 的 `outbound_default_headers()`，把 `User-Agent`
固定为 `dataops-troubleshooter/1.0`。这不是风格偏好而是实测兼容性结论：OpenAI-compatible 第三方
网关前常挂 WAF，会按客户端标识拦截。同一密钥、同一模型、同一 strict JSON Schema 下逐项对照的
结果是——裸 `httpx` 请求返回 200，只把 `User-Agent` 换成 SDK 默认的 `OpenAI/Python 2.9.0` 就返回
`403 Your request was blocked.`，而 SDK 的 `x-stainless-*` 遥测头全部加上仍然返回 200。因此 403
既不是密钥、配额问题，也不是 Structured Outputs 不被支持。请求仍带 Bearer 令牌、仍走配置的
base_url，语义没有任何改变。把它放在单独模块而不是各 Provider 内联，是为了让 Planner、Auditor、
Embedding、Reranker 四个出站入口不可能漏改：否则下一个新增 Provider 会在真实网关上重现同一个
难以定位的 403。

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

`auditor-report:v2` 与 Planner 一样拆成静态 system 和不可信 user 数据。Renderer 注入报告、实时
Evidence/ToolEvent、GraphRAG Bundle、confirmed cases、确定性 history_case_matches、capability 规则、
issues 和审计轮次。Auditor 只能返回 `AuditResult` 的 accept/revise，有限问题码防止创建新状态。

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

### 13.5 confirm、reject、重新 confirm 与 confirmed-only 向量/图融合搜索

状态决策是显式有限枚举：`confirm` 映射 `confirmed`，`reject` 映射 `rejected`。同一目标状态幂等；
confirmed 与 rejected 可以相互切换以支持取消确认和纠错，但 API/Service 不提供恢复 pending 的
隐式动作。不存在的 memory 返回 404；数据库未配置时两个记忆 API 都返回 503，明确区别“能力未
启用”和“搜索成功但没有命中”。

搜索先校验非空 query 和 1..20 的 limit，再使用当前 Provider 生成单个固定维度向量。第一条 SQL
在相同 Provider/维度且 `status='confirmed'` 的案例中取 pgvector 直接 top-k。第二条 SQL 从这些
种子的动态 case 节点沿 `case-memory-graph:v1` 拥有的 `SIMILAR_TO` 出边扩展邻居，并再次校验邻居
仍为 confirmed 且向量空间兼容；手工边、pending/rejected 和旧 Provider 都不能进入。

图传播分使用 `seed_similarity * edge.weight`。边权只说明两个历史案例相似，乘本次查询对种子的
直接分后，图关系才与当前问题绑定；这避免一组彼此相似但与本次告警无关的旧案例仅凭图结构得到
高排名。直接候选和图邻居按 memory ID 去重：同一案例可同时保留 vector/graph 两个通道，最终
similarity 取两路最大值；多条图路径只保留最高图分，并在并列时保留全部稳定 edge ID。最终按
最终分、直接分、图分、更新时间和 memory ID 稳定排序后裁剪 limit。

`case-memory:v2` raw match 返回公开 CaseMemory、`retrieval_channels`、`direct_similarity`、
`graph_score` 和 `graph_edge_refs`，不返回 embedding。Pydantic 联合验证通道与分量、edge ID 前缀和
最终分一致性，形成 SQL 过滤之外的第三道防线。确定性 matcher 只把 case ID 与实时 TOOL Evidence
作为报告引用；edge ID 用于解释检索路线，不伪装成实时 Observation。Planner/历史 capability 仍
必须让本次实时 Observation 优先，历史案例只能作为参考。

如果部署切换 Embedding Provider，旧案例不会跨空间参与搜索；当前切片没有批量重嵌入迁移。精确
签名再次命中某条旧案例时会用当前 Provider 重算该条向量，但完整 Provider 切换仍应先提供显式的
离线重嵌入与审计命令，不能直接修改 Provider ID 冒充向量兼容。

### 13.6 confirmed 案例注册 GraphRAG 与 `SIMILAR_TO`

`PostgresCaseGraphRegistrar` 是确定性持久化组件，不是第三个 Agent，也不调用 Chat/Embedding
Provider。它只接收仓储已经验证的 `StoredCaseMemory`，把 `mem_<16hex>` 稳定映射为
`case_<16hex>` 节点。节点类型固定为 `case`，`source_id` 保留原 memory ID，正文按症状、根因、
故障路径、方案、组件、标签和证据引用的稳定顺序生成；超出知识节点上限时从 4000 字符处裁剪，
并把根因/症状放在前部以保留主要检索语义。节点 reliability 固定为 0.9，表示“用户确认且来源可
追踪的结构化案例”，但不把历史结论提升为实时事实。

案例节点直接复用 `case_memories.embedding`、`embedding_provider` 和 `embedding_dimensions`。
确认动作不会再次访问远端 Provider，因而记忆搜索和 GraphRAG 节点始终位于同一数学空间；内部
向量仍不进入 API、Prompt、日志或事件。upsert 前会锁定同 ID 节点，并要求已有节点同时满足
`node_type=case` 和 `source_id=memory_id`。若人工种子或其他来源占用了该稳定 ID，注册显式失败而
不是覆盖来源，外层事务随后回滚用户确认。

图相似阈值使用独立配置 `DATAOPS_CASE_GRAPH_SIMILARITY_THRESHOLD`，默认 0.75，并由 Settings
保证不高于记忆去重阈值 0.92。两者必须分开：去重回答“是否是同一 canonical 案例”，图关系回答
“两个保留案例是否足够相似可供参考”；若两个阈值完全相同，达到相似边阈值的同组件候选通常已经
先被合并，案例图会很难形成有意义关系。邻居查询只比较 confirmed、相同 Provider 和相同维度，
排除自身及零相似度，并用每节点最多 20 个邻居控制小型图写放大。

PostgreSQL 的边是有向的，而案例相似在业务上对称，因此每对案例写 A→B 与 B→A 两条
`SIMILAR_TO`。方向、两端节点和来源版本 `case-memory-graph:v1` 共同生成稳定 SHA-256 短 edge ID；
weight 保存裁剪后的 cosine similarity，`source_span` 明确记录两个节点和分数。每次注册先删除本
组件拥有且触及当前节点的旧相似边，再按最新向量快照重建，保证重复 confirm、重新 confirm 和已
confirmed 案例后续合并都幂等且不遗留过期关系。候选旧案例节点会在写边前渐进 upsert，因此升级
前已经 confirmed 但尚未注册的相似邻居也能被补齐。

`PostgresMemoryRuntime.decide()` 在一个 `AsyncSession.begin()` 内依次执行状态变更和图同步：
confirm 读取内部快照并注册节点/边；reject 删除动态节点，`knowledge_edges` 两端外键的
`ON DELETE CASCADE` 原子清除所有入边/出边。`stage()` 也会在重复报告合并到已 confirmed 案例后
重建图，因为该合并可能改变正文和 embedding。节点冲突、pgvector、唯一约束、外键或 Pydantic
任一步失败都会回滚状态/合并与图写入，不允许出现“confirmed 但没有图节点”或“记忆已更新但图
仍使用旧向量”的部分成功。

在线 `GraphRetrievalService` 现在把 `SIMILAR_TO` 加入批准关系白名单。case 节点可以通过全文或
相同 Provider 的 pgvector 成为种子，再沿真实双向边扩展为带 node/edge/source/path_id 的证据路径。
历史案例搜索现在复用相同 case 节点与边，把 pgvector 直接 top-k 和 SIMILAR_TO 邻居按 v2 契约
合并。通用 GraphRAG 返回完整 path_id；历史 matcher 则保留更紧凑的 edge refs 和传播分，两个入口
共享图事实但服务不同上下文预算，不能把 edge 引用误称为已执行的实时工具证据。

### 13.7 生命周期、健康检查与当前范围

FastAPI lifespan 仅在 PostgreSQL 配置并通过连接/种子检查后创建 `PostgresMemoryRuntime`。Runtime
只保存 session factory、Embedding Provider 和预算；每次写入创建独立事务，每次查询创建短只读
会话，避免跨请求共享非线程安全 `AsyncSession`。`/health` 报告契约、Provider、维度、去重阈值、
图相似阈值、默认 limit 及 pending/confirmed/rejected 计数，但不公开数据库 URL、密码或 embedding
内容。知识节点/边数字仍是 lifespan 启动快照；动态案例图的实时规模通过 PostgreSQL 专项查询验证，
后续若 UI 需要实时图计数再单独设计读接口，避免高频健康探针放大数据库负载。

本切片已经实现并验证候选构建、暂存/合并、两阶段去重、确认状态、默认搜索和 API。顶层工作流
还会把 raw matches 交给 `explain_case_matches`，生成共同点、差异点、参考动作、避坑提示和引用，并
投影到 Planner/Auditor/报告。GraphRAG `case` 节点、双向 `SIMILAR_TO`、拒绝清理、真实路径召回
和 history matcher 图邻居合并已经接通；删除案例 API 也会在同一事务清理证据关联与动态图节点。
尚待模型 Provider 的复杂语义解释不能被确定性 matcher 冒充为通用自然语言理解。

### 13.8 验证方式

- 单元测试覆盖审计资格、exact/vector 去重、向量/图候选合并、graph-only 替换较弱直接候选、通道/评分 Schema 和实时事实优先解释。
- API 集成测试覆盖数据库禁用 503、confirm/reject、`case-memory:v2` 检索来源字段和健康计数刷新。
- PostgreSQL 集成测试真实执行 pgvector direct top-k、SIMILAR_TO join、传播分排序、图候选进入 top-k、reject 后立即移除，以及节点/边事务行为。
- `tests/unit/test_documentation_policy.py` 把 Service、Repository、Runtime、Graph Registrar 和迁移列为关键边界文件，要求 callable docstring 与关键步骤内联注释同步存在。

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

`audited-diagnosis-workflow:v2` 因此使用第三层确定性 LangGraph 组合五个节点：

```text
recall_case_memories -> run_react -> explain_case_matches -> run_report -> stage_case_memory
```

它不是第三个 Agent。节点只调用现有协议对象，Planner 与 Auditor 仍是唯二 LLM 角色；顶层状态只
保存 Pydantic 请求、raw matches、解释结果和三个子结果，Provider、数据库 session factory 等
不可序列化依赖通过 LangGraph runtime context 注入。

### 14.2 历史按需触发和查询预算

`CapabilitySelectionRequest.history_trigger` 是唯一查询开关。`not_requested` 直接跳过数据库；
`user_requested`、`planner_validation`、`reusable_signature` 才调用记忆 runtime。这样历史匹配仍是
第五项按需 capability，而不是每个诊断固定支付的向量查询。

查询由 `_build_memory_query` 确定性构造，顺序是用户问题、本次非 CASE_MEMORY Evidence、当前假设。
实时 Observation 位于假设之前，使字符预算截断时优先保留本次事实；CASE_MEMORY 来源被排除，避免
旧案例内容递归查询并强化自身。`memory_query_max_chars` 默认 4000，`memory_search_limit` 默认 5，
都由 Pydantic 限制并在 `.env.example` 说明。

### 14.3 两阶段确定性案例解释与实时 Observation 优先

记忆搜索返回 `CaseMemoryMatch` 的 memory、最终分、vector/graph 通道、直接分、传播分和 edge refs，
响应模型拒绝 pending/rejected 及通道/分量矛盾。ReAct 前，
matcher 用初始 query/组件/旧 Evidence 生成 preliminary `SimilarCaseReference` 给 Planner；ReAct 后，
`explain_case_matches` 对完全相同候选重新比较终态 Evidence 与假设，再交给报告和 Auditor。两次均
不查询数据库、不改变顺序或 similarity，因此不会产生“Planner 与 Auditor 看见不同候选”的漂移。

比较规则先说明最终排序分、直接 pgvector 分或 SIMILAR_TO 图传播分都只是候选排序，再比较组件
交集/差集、保守症状文本包含和当前
非 rejected 根因。根因不一致会写入 differences，并加入“禁止直接复用”警告；没有可比较根因时
明确标注待验证。历史 solution_steps 只进入 reference_actions，绝不生成已执行事实。evidence_refs
强制包含 case_id，并补充最多七条本次 TOOL Evidence，保证旧先例与实时事实两侧都可追溯。

`SimilarCaseReference` 强制 confirmed 和五类非空解释，列表不得重复。React/Report/Auditor 上下文
校验 raw memory ID 与 match case_id 顺序一致；DiagnosisRunResult 再校验 similarity 未被修改。
ReportPolicyValidator 要求最终报告项与 matcher 完全相等，防止模型或修订节点提高分数、删除冲突
或改写历史方案。实时 Evidence 仍先进入引用索引，历史 case_id 只能作为较低优先级参考来源。

### 14.4 审计后 staging 与失败语义

`stage_case_memory` 永远位于 report 子图之后。顶层不读取自然语言判断是否 accepted，而是把完整
`ReportRunResult` 交给 `CaseMemoryService`：accepted 有根因时 staged/merged，accepted 无根因时
`skipped_no_root_cause`，degraded 时 `skipped_not_accepted`。最终 `DiagnosisRunResult` 还校验 ReAct
与 report 的 run/session 相同，并禁止 degraded 搭配写入成功状态。

staging 完成后，顶层会重新通过 `ReportRunResult` Schema 构造终态，并把 `stage.memory` 同步到
`AgentState.memory_candidate`；跳过时则明确清空该字段。最终结果再次要求 state 内候选与外层
`memory_stage.memory` 相同，避免未来 checkpoint、run API 和直接 workflow 调用观察到两套状态。

最终报告的 evidence_refs 会包含 similar case ID，方便 UI/Auditor 追溯历史来源；但长期记忆候选
不能把该 ID 再写入自身 Evidence。`_candidate_from_result` 在投影前收集所有
`report.similar_cases.case_id` 并显式排除，只保留本次实时、知识节点或图路径引用。否则 exact merge
会形成 memory 自引用并让历史案例递归证明自身。过滤后没有证据会明确失败，不降级成自我引用。

历史搜索错误在任何 Agent 调用前传播；ReAct、报告或 staging 的编程/数据库错误同样不吞掉。
这是有意的失败策略：无历史命中是正常空列表，依赖故障不是空列表；持久化状态未知时也不能向
调用方返回“诊断完整完成”。未来 HTTP 层应把这些异常映射为明确运行失败事件，而不是改成 200。

### 14.5 验证范围与当前限制

单元测试使用记录型协议替身验证 trigger、查询优先级/截断、同一案例跨子图复用、degraded 跳过和
搜索异常短路。PostgreSQL 集成测试运行真实顶层图、真实 ReAct/报告 LangGraph 和真实 memory
runtime：首个 session 暂存并确认，第二个 session pgvector 召回，Planner/Auditor 均收到案例，
最终 exact signature 合并且 occurrence_count 从 1 增至 2。

当前已提供 `/api/v1/sessions`、message/run/event 资源、顶层 runtime、session checkpoint 恢复、
Worker 取消/恢复和 memory 删除；GraphRAG `case`/`SIMILAR_TO` 已在记忆决策事务接通，history
matcher 合并图邻居并把同批候选交给 Planner/Auditor。模型级复杂语义对比仍保留接入点，当前
确定性规则只覆盖可客观比较的字段，不宣称理解任意自然语言冲突。

## 15. 资源化诊断 API

### 15.1 为什么使用 PostgreSQL Worker 而不是进程内后台任务

产品要求 GET run/events 可轮询；FastAPI `create_task` 或 BackgroundTasks 在进程重启后无法恢复，
也没有跨 worker 所有权、取消和重试语义。因而当前使用 PostgreSQL 持久化队列与 lease，把“可恢复”
作为真实生命周期而不是 UI 假象。

`diagnosis-resources:v4` 的 POST message 只创建 queued run 并返回 HTTP 202；Worker 使用
`FOR UPDATE SKIP LOCKED` 领取任务，完成/失败/事件/checkpoint 在同一事务提交。取消使用行锁把
queued/running 转为 cancelled，恢复只允许从 cancelled 来源创建新 queued run，保证旧时间线可审计。

### 15.2 四张表和数据库状态机

迁移 `20260715_0004` 创建：

- `diagnosis_sessions`：稳定 session ID、标题、最后问题 500 字符摘要、创建/更新时间。
- `agent_runs`：run/session、用户问题、显式 intent/components/history trigger、status、JSONB result、
  安全错误和生命周期时间。
- `run_events`：稳定事件 ID、run、连续 sequence、phase/type、summary、JSONB 安全 payload 和时间。
- `session_checkpoints`：session 主键、来源 completed run、单调 checkpoint_version、版本化 JSONB
  快照和创建/更新时间。

`agent_runs` CheckConstraint 把三种状态与 payload 原子绑定：running 不能有结果/错误/completed_at；
completed 必须有 result 且无错误；failed 必须有 error_code/message/completed_at 且无部分 result。
`run_events(run_id, sequence)` 唯一，领域 `RunEventList` 再要求 1..N 连续，形成数据库与响应双防线。

### 15.3 请求路由和真实执行链

`DiagnosisMessage` 要求 content、intent、components 和可选 history_trigger。当前没有经过验证的自然
语言路由分类器，因此不让 API 猜意图；模型通过 `CapabilitySelectionRequest` 复用单/跨组件数量和
重复组件校验，无效请求在创建 run 前返回 422。

完整 user_query 最多 4000 字符并原样进入 AgentState；`graphrag-retrieval:v3` 查询字段上限为 2000，
所以 `PostgresGraphContextRetriever` 在去空白后只截断检索副本，不修改持久化问题或 Planner 输入。
这样合法长消息不会因 GraphEvidenceBundle Schema 产生 failed run，也不会静默缩短用户上下文。

配置 PostgreSQL 和 OpenAI-compatible Planner/Auditor 后，lifespan 组装：

```text
PostgresGraphContextRetriever
  -> BoundedReactLoop(McpToolExecutor)
  -> AuditedReportWorkflow
  -> AuditedDiagnosisWorkflow(memory runtime)
  -> DiagnosisApplicationRuntime(run repository)
```

Provider disabled 或数据库缺失时 diagnosis runtime 不发布，四个资源路由统一返回 503。构造阶段只
审计本地配置，不发送付费探测；真正模型调用只发生在 POST message。

### 15.4 事务拆分、失败持久化和事件安全

创建 session、创建 running run、完成 run、失败 run 分别使用短事务。GraphRAG、LLM 和 MCP 等长
I/O 位于事务之间，不持有数据库连接或行锁；完成时 `FOR UPDATE` 要求 run 仍为 running，然后一次
写 JSONB DiagnosisRunResult、整批事件和最新 checkpoint。这样避免慢模型阻塞轮询，也阻止重复
提交覆盖终态或让 completed run 与追问状态不一致。

workflow 或检索异常会在新事务中把 run 标为 failed，并写 sequence=1 的 system event；随后抛出
`DiagnosisExecutionFailed`，HTTP 500 只公开 run_id、`diagnosis_execution_failed` 和通用摘要。原始
异常通过 Python chaining 保留但不进入表/API，数据库/失败事务自身出错也不会被吞掉。

成功事件固定按 retrieval → React 原顺序 → report 原顺序 → memory → checkpoint 排列。retrieval
payload 记录恢复版本/来源 run，最后 system 事件记录新版本；均不序列化 AgentState 全量、Prompt、
Thought、embedding 或异常文本。

### 15.5 短期 checkpoint 的构建、恢复和失败语义

迁移 `20260716_0005` 创建 `session_checkpoints`。每个 session 只保存最新快照，`source_run_id` 唯一
关联产生它的 completed run，`checkpoint_version` 必须从 1 开始逐次加一。完成事务先锁 running run，
再锁当前 checkpoint；版本跳跃表示并发 run 基于旧上下文完成，会回滚并进入安全 failed 路径，不能
倒退会话状态。失败 run 永远不写 checkpoint，因此最后一个成功上下文仍可用于下一次追问。

`SessionCheckpoint` 使用显式字段白名单保存 plan、hypotheses、Evidence、ToolEvent、retrieved_paths、
observation_refs 和最终公开 DiagnosisReport。它不直接序列化 AgentState，因此未来新增字段不会自动
进入存储；next_action、stop_reason、audit_result、memory_candidate、旧 react_step、Prompt、Thought、
原始模型输出和 embedding 均被排除。

为防止长会话无限膨胀，`app/memory/checkpoint.py` 将 plan/hypotheses/Evidence/ToolEvent/路径/引用
分别限制为 16/16/64/64/32/128 条，并保留最新尾部；observation_refs 从后向前去重后恢复时间顺序。
这些上限是模块级可审计常量，测试用超限合成序列验证淘汰边界。它们只约束短期上下文，长期事实仍
通过经过 Auditor 的 `case_memories` 保存。

恢复函数校验 session 所有权后创建新 AgentState：run_id/user_query 使用本轮值，react_step/retry_count
归零，终态字段清空；上一报告投影为 `SessionTurnContext` 进入 Planner v3 user 消息。GraphRAG 查询
按“当前问题 → 上一问题 → 上一摘要 → 上一根因”组合，帮助省略式追问保持主题，但实时 Observation
仍优先。ToolEvent 被保留用于跨 run Action 去重，指纹忽略 trace_id 以防新 run 绕过。

### 15.6 当前限制与验证

执行模式为 PostgreSQL Worker；支持 queued/running/completed/failed/cancelled、有限重试、取消和
run-level 恢复。checkpoint 使用固定上限的滚动窗口保留最新 plan/Evidence/ToolEvent/路径/引用，
避免 JSONB 随会话无限增长；它不等同于 LangGraph 框架内部逐节点中断恢复，后者仍是模型/框架接入
边界。session 列表与分页不在产品首版范围内。

取消与完成竞争时，`cancel_run` 和 `complete_run` 都对 agent_runs 行加 `FOR UPDATE`；先提交的一方
赢得终态，后提交方因状态/lease 不匹配而不能覆盖结果。Worker 看到 lease 续租失败会停止执行子任务，
因此取消不会产生第二条失败事件或半成品 checkpoint。

单元测试覆盖 message 路由、run 状态组合和事件连续性；API 测试覆盖 disabled 503、成功 Schema、
404 和安全 500；PostgreSQL 测试覆盖迁移、session 摘要、completed JSONB、连续事件、failed 安全
摘要、checkpoint v1→v2、跨会话隔离、失败不覆盖和 CheckConstraint。Docker 默认 Provider disabled，
而不是冒充可执行模型诊断。

## 16. 测试分层

- 单元测试：Pydantic 约束、Fixture、Planner/Auditor Prompt 与 Structured Output 修复、Observation、固定 capability registry、LangGraph ReAct/报告返工门禁、Provider 稳定性、向量元数据、混合评分、Evidence Bundle 预算和消融 Schema。
- 模型/MCP/编排集成测试：官方 AsyncOpenAI MockTransport、真实 stdio 握手、九工具发现、成功/失败响应、重试 trace、Planner → Action → Observation → Planner 回环，以及规则否决 → Auditor → 唯一报告返工。
- PostgreSQL 集成测试：迁移、pgvector 扩展、带 Provider 溯源的幂等种子、cosine/全文双路检索、混合评分、预算 Bundle、vector-only/vector+graph、删边消融，以及案例记忆去重/幂等/确认召回。
- Docker 验证：从镜像安装依赖，等待 PostgreSQL 健康，执行迁移/种子，再检查 API `/health`。

### 16.1 长期记忆召回消融评测

`data/evals/memory_recall_cases.json` 使用 `memory-recall-eval:v1` 定义五条合成 corpus 和六条查询：
图邻居救回、直接向量基线、rejected 案例隔离。corpus 的 root cause 与查询分别绑定确定性角度键；
集成测试 Provider 把角度转为八维单位向量，但距离、直接 top-k、图边 join、传播分和最终排序仍由
生产 PostgreSQL/pgvector 与 `PostgresMemoryRuntime` 执行。

`MemoryRetrievalMode` 只有 `vector_only` 与 `vector_graph`。Runtime 默认始终是后者；公开 API 不
接受 mode 参数，因此消融能力不会让用户绕过生产图召回。评测器对每条 case 顺序运行两个模式，
唯一变量是是否沿 `SIMILAR_TO` 扩展，query、limit、corpus、Provider、阈值均保持一致。若
vector-only 返回 graph 通道，评测立即失败，防止对照组名义关闭、实际仍扩图。

指标定义如下：Recall@K 是 expected 命中数除以 expected 总数；Precision@K 是 expected 命中数
除以实际返回数；graph-only hit 要求候选有 graph 通道且没有 vector 通道；forbidden hit 单独统计
rejected 等安全负样本。逐案例还记录 graph rescued label 与 regression label，suite 使用 macro 平均
让六条查询等权。所有报告固定 `metric_kind=measured`，不调用 LLM，不声称最终报告准确率。

真实 PostgreSQL 实测记录见 `docs/memory-recall-eval-results.md`：当前六条查询中 Macro Recall@K
和 Precision@K 均从 0.9167 变为 1.0000，禁止案例命中为零。该 +0.0833 只适用于小型角度 fixture，
不能外推为通用提升；替换模型、数据或预算后必须重跑。

### 16.2 历史案例端到端影响消融评测

检索层 Recall 提高并不能自动证明 Planner 行为或最终报告更好，因此 `history-impact-eval:v1` 在
`app/orchestration/history_evaluation.py` 增加第二层 Memory off/on 消融。`data/evals/history_impact_cases.json`
固定三条合成诊断：历史引导必要 Action、历史根因与实时事实冲突、同根因稳定参考。每条 case 同时
标注意图输入、组件、scenario、必要/可选工具、允许/禁止根因、最小历史命中和是否要求冲突保护。

调用链如下：

```text
load_history_impact_eval_suite
  -> runner.run(case, memory_off)
  -> runner.run(case, memory_on)
  -> validate paired trigger/query/history boundary
  -> read actual ToolEvent and audited DiagnosisReport
  -> calculate per-case delta and macro measured report
```

必要 Action 从实际 `ToolEvent` 而不是 Planner 摘要提取，因此被 capability、trace、重复调用或引用
门禁拦截的 Action 不会虚增覆盖。根因实时引用率只认可本次 TOOL Evidence；案例 ID、相似度或
`SIMILAR_TO` edge 只能说明历史来源，不能单独支撑当前根因。memory-on 的 raw memory ID 还必须按
原顺序完整进入 `DiagnosisReport.similar_cases`，否则历史投影失败。

冲突案例进一步从 raw `CaseMemory.root_cause` 找到 forbidden 历史根因，并要求对应确定性解释同时
包含“根因不一致/冲突”和“禁止直接复用”提示。最终报告若采纳 forbidden 根因、根因缺少 TOOL 引用
或冲突提示被删，都会计入 realtime priority failure；这比只断言“报告有 similar_cases”更能证明
旧经验没有覆盖本次事实。

`tests/integration/test_history_impact_langgraph.py` 运行真实 `BoundedReactLoop`、
`AuditedReportWorkflow` 和 `AuditedDiagnosisWorkflow`。Planner/Auditor 使用确定性协议替身，工具响应
仍经过生产 `normalize_observation`，因此 Action、Evidence、ToolEvent、Builder、规则 Validator、
Auditor 和 staging 都走真实边界。历史搜索使用合成 confirmed match；真实 PostgreSQL/pgvector
召回准确性已由 16.1 单独验证，两个实验不混用变量。

当前三案例实测记录在 `docs/history-impact-eval-results.md`：必要 Action macro 覆盖从 0.6667 变为
1.0000，意外 Action macro 率从 0.3333 变为 0；Memory off/on 的 Top-1 根因命中与 TOOL 引用率都为
1.0000，历史投影和冲突保护通过率也为 1.0000。这些结果只证明固定脚本下编排和安全契约生效，
不能外推为真实模型准确率、时延或成本提升。

### 16.3 独立 Auditor 增量影响消融评测

确定性规则擅长检查 ID 是否存在、假设状态、引用集合、结构化冲突字段、风险字段和历史匹配是否
漂移，但不能可靠判断自然语言 Evidence 是否真的支持根因，或一个字段齐全的动作在当前语境下是否
仍然危险。`auditor-impact-eval:v1` 在 `app/orchestration/auditor_evaluation.py` 中把这类语义问题与
规则问题分开测量。

评测中的 `auditor_off` 不是生产开关。它只调用同一 Builder 和生产 `ReportPolicyValidator`，保留
原始草稿并标记 `control_unreviewed`；不能称为 accepted，也不能写入长期记忆。`auditor_on` 才运行
完整 `AuditedReportWorkflow`。评测器要求两组 initial draft 和 deterministic issues 完全一致，而且
规则问题必须为空；如果规则已经发现缺陷，案例应归入 Validator 测试，不能重复宣传成 Auditor
增量收益。

`data/evals/auditor_impact_cases.json` 固定三类合成语义缺陷：

1. 引用 ID 与 supported hypothesis 对齐，但 Evidence 内容实际不支持根因；
2. 另一条 TOOL Observation 与根因冲突，但没有预先写进 `contradicting_evidence`；
3. 修复步骤具备风险枚举、前置、回滚和验证字段，但仍包含“直接覆盖目标表”的危险语义。

调用链如下：

```text
load_auditor_impact_eval_suite
  -> runner.run(case, auditor_off: builder + validator only)
  -> runner.run(case, auditor_on: audited-report-workflow:v2)
  -> compare identical draft and deterministic precheck
  -> calculate issue detection, unsafe retention and safe resolution
```

问题发现率只统计 fixture 预期的有限 `AuditIssueCode`，额外多报问题不会提高分数。危险残留直接检查
最终 `DiagnosisReport.root_causes` 与 remediation action marker；因此即使 Auditor 返回 revise，如果
`SafeReportReviser` 或降级没有真正删除危险内容，safe resolution 仍为失败。accepted 和 degraded
分别计数，安全降级不能包装成审计通过。

`tests/integration/test_auditor_impact_langgraph.py` 使用同一 case-specific Builder 和生产 Validator。
on 组运行真实报告 LangGraph、生产 `SafeReportReviser`、第二轮审计和降级节点；结构化 Auditor
替身只提供有限 issue，不修改报告。三例 deterministic precheck 均为空，三例均执行一次修订；
unsupported 与风险案例二审接受，持续证据冲突案例二审仍拒绝并降级。

实测记录见 `docs/auditor-impact-eval-results.md`：预期问题发现率从 0 变为 1.0000，危险内容残留率
从 1.0000 降为 0，安全处置率从 0 变为 1.0000；三例均属于规则未命中后的 Auditor 增量发现，
最终 2 accepted、1 degraded。该小样本使用确定性 Auditor 脚本，只证明职责分离和控制流生效，
不能外推为真实模型的语义判断准确率、误报率或成本收益。

### 16.4 统一作品集评测 manifest 与单命令运行器

四层消融此前各有独立测试和实测文档，但使用者需要记住不同命令，且失败后仍可能手工复制旧数字。
`portfolio-eval-manifest:v24` 把这些层与 Golden 诊断基线的 suite ID、来源契约、结果文档、PostgreSQL 前置、受限 pytest target 和
已审核指标快照集中到 `data/evals/portfolio_eval_manifest.json`。它只汇总现有实测，不把不同层计算
成一个没有统计意义的总准确率。

`app/evaluation/portfolio.py` 实现 `portfolio-eval-run:v23`。测试 target 必须是仓库内 `tests/*.py`
文件或测试节点，manifest 不能加入任意 flags；真实执行使用当前 Python 解释器和
`subprocess.run(shell=False)`，stdout/stderr 被捕获后只在失败层提供截断摘要。运行器不会输出环境变量、
数据库 URL、Prompt、Thought 或供应商响应体。默认文件和 CLI 使用五层 v24；v1 精确四层与 v2/v3/v4/v5/v6/v7/v8/v9/v10/v11/v12/v13/v14/v15/v16/v17/v18/v19/v20/v21/v22/v23
Golden v1/v2/v3/v4/v5/v6/v7/v8/v9/v10/v11/v12/v13/v14/v15/v16/v17/v18/v19/v20/v21/v22 来源仍可兼容读取，旧结果不会被静默解释为新完整运行。

调用链如下：

```text
python -m app.evaluation
  -> load and validate portfolio-eval-manifest:v24
  -> verify result documents and pytest targets exist
  -> run each suite with a bounded subprocess timeout
  -> publish snapshots only for status=passed
  -> render portfolio-eval-run:v23 JSON to stdout
```

状态语义刻意区分四类：`passed` 才携带 metrics；`failed` 表示 pytest 已执行但失败；`skipped` 只来自
显式 `--skip-postgres`；`blocked` 表示请求完整运行但缺少 `DATAOPS_TEST_DATABASE_URL`。failed、skipped
和 blocked 均隐藏 snapshot，防止旧数字冒充本次成绩。`run_success` 表示没有 failed/blocked，
`complete` 表示没有 skipped/blocked，`all_suites_passed` 只有五层全 passed 才为真。

默认完整命令为：

```powershell
$env:DATAOPS_TEST_DATABASE_URL='postgresql+asyncpg://...'
.venv\Scripts\python -m app.evaluation
```

`.venv\Scripts\python -m app.evaluation --skip-postgres` 运行 History/Auditor 两层 LangGraph 评测和
Golden 确定性回归，用于无 Docker 环境的快速反馈；即使退出码为零，JSON 仍明确 `complete=false` 和
`all_suites_passed=false`。`tests/integration/test_portfolio_evaluation_cli.py` 会真实启动该 CLI，并验证
两个 skipped suite 没有 metrics。

`app/evaluation/golden_diagnosis.py` 实现 `golden-diagnosis-eval:v23`。它依赖可替换异步 runner，但评分
只读取完整 `DiagnosisRunResult`：意图来自终态 state，必要 Action/重复率/成功率来自 ToolEvent，关键
来源来自 Evidence，Top-1、引用、最高风险和安全降级来自最终审计报告。合法 `attempt=2` 重试不算重复；
根因、链路和高风险建议的引用做两项稳定 ID 检查——悬空判定直接调用报告层
`collect_reference_sources`（因此 Bundle 知识节点与文档切片同样合法），实时支撑判定要求至少一条引用
落在本次 Observation、可引用图路径或已确认案例上——语义支持仍由 Auditor 与人工抽查负责。v21 只有
一个把两者混起来的 AND 条件，且宇宙不含 Bundle 知识节点，所以"多引用一条合法知识依据"会被误判为
悬空引用，这是 v22 的唯一行为差异。

v23 只增加 `root_cause_anchor_hit_rate` 一个指标，既有指标定义一字未改。它读 Top-1 根因引用里的
`kn_root_cause_*`：因为 `app/retrieval/budget.py` 的 `KNOWLEDGE_EVIDENCE_ID_PREFIX` 把知识节点的
evidence_id 固定生成为 `kn_<node_id>`，一条引用就精确编码了知识图节点 ID，"报告指向哪个故障模式"
因此可以纯离线精确判定，不需要对自然语言根因文本做相等或相似度比较。计数前该条结论必须先通过与
关键结论完全相同的两道校验（全部引用非悬空、至少一条落在实时支撑集合），否则凭空编造节点 ID 或
只堆静态知识都能刷出命中。它与文本相等的 `root_cause_top1_hit_rate` 分母不同（14 条声明锚点案例对
21 条有根因案例），是两个必须并列发布的独立指标：把后者的低分换成前者的高分并宣称"提升"就是改
口径冒充改进。`anchored_case_count` 由报告层不变量从明细复算，避免分母被手工填成案例总数。

v8 到 v9 只新增一个输入字段 `requested_components`，评分规则、指标定义和分母一字未改，因此
`docs/golden-diagnosis-eval-results.md` 的 28/28 与 `docs/live-golden-eval-results.md` 的 Run A–G
数值继续有效、也不允许因为升版被改写。该字段声明"用户在界面勾选了哪些组件"，加载阶段强制它与
`expected_intent` 元数自洽（`single_component_diagnosis` 恰好一个、`cross_component_diagnosis` 至少
两个），并强制全部 `required_tools` 落在这个范围内。触发它的是一次真实失败：18 个 Fixture 被 28 条
案例复用，六条单组件案例共用同一个三组件场景，旧实现从 Fixture 推导组件，于是 `--all-cases` 跑到
第六条案例时才被 `CapabilitySelectionRequest` 的元数校验拒绝——前五条案例的真实模型费用已经花掉，
而失败运行按设计不写任何报告。现在这条不变量在读 JSON 时就成立，确定性 runner 与真实模型 runner
都直接读同一个字段，`tests/unit/test_live_golden_evaluation.py` 另有一条零成本用例逐条构造 28 个
生产消息。组件范围是输入而不是答案：工具名、允许根因、必要证据来源和停止原因仍然不进入消息，
测试只对 runner 追加的路由段做泄漏断言，因为 `user_query` 本身是用户的措辞（有一条记忆案例的
问题里就写着"FlashSync 主键冲突"）。

v9 到 v10 同样不改评分规则、指标定义和分母，只把记忆标注里的 memory ID 收紧成生产真能铸造的形状：
`GoldenMemoryId` 要求 `mem_` + 16 位小写 hex，`GoldenMemoryExpectation.memory_id` 与
`GoldenHistoryExpectation.forbidden_memory_ids` 共用这一个别名，因此 required 与 forbidden 不可能被改成
两套宽严不同的口径。触发它的是另一次真实失败：`--seed-history` 第一次执行时在
`app/memory/graph_registration.py` 的 `case_graph_node_id` 抛出 `case graph memory_id must use
mem_<16 hex> format`。生产铸造 ID 的唯一路径是 `app/memory/service.py` 的 `mem_{signature[:16]}`，
而图注册器依赖这个形状构造可逆的 `case_<16hex>` 主键；旧标注写的是 `mem_lts_dependency_stall` 这类
可读 ID，等于声明了一个真实系统永远建立不出来的前置条件。确定性替身的 `RecordingMemoryRuntime.decide`
不校验 ID 形状，所以这个谎言在离线回归里完全不可见。修法刻意选择收紧数据集而不是放宽
`case_graph_node_id`：后者会破坏 `case_<16hex>` ↔ `mem_<16hex>` 的可逆溯源、改写已有图主键，等于为了
测试方便削弱生产约束。五个 ID 由案例语义经 blake2s 派生（`mem_ebc78324034714d6` /
`mem_43fb5df2a9cf66da` / `mem_4c0ab7ebba8e2aaa` / `mem_fdd472fc47cd485d` / `mem_079acbd5fc8f2fbc`），
评分器仍按精确字符串比较 memory ID，所以只做"评测期 ID 映射"会让分母与真实库脱钩，因此被否决。

`golden-case:v10` 的 `case_category` 当前为 8/10/4/3/3。28 条案例使用 18 个 Fixture；三条记忆案例
增加 `history_expectation`，逐项保存 required memory ID、历史根因、相似度、冲突标记和 forbidden IDs。
runner 构造 confirmed `CaseMemoryMatch` 与同 ID/相似度的 `SimilarCaseReference`，评分器检查触发、召回
覆盖、confirmed-only、报告投影和实时优先。即使旧根因只引用有效 memory ID，若没有本次 TOOL
Evidence 或与允许根因冲突，实时优先仍失败。

新增的 `GoldenEvidenceConflictExpectation` 专门描述“协议和工具调用均成功，但业务事实无法同时为真”
的边界。它不把冲突检测硬编码为中文关键词，而是由 Golden Case 显式列出至少两个稳定 source ID、
禁止根因，以及是否要求空根因和公开 uncertainties。Schema 先验证冲突来源是
`required_evidence_sources` 的子集，再验证禁止根因不与允许根因重叠；要求无根因时
`allowed_root_causes` 必须为空。这样的校验顺序把数据集矛盾挡在运行前，评分器不需要猜测标注意图。

第 12 条 `bds_conflicting_partition_evidence` 使用三个均为 `ok=true` 的 BDS 响应：状态声称源分区
未就绪，日志声称读取完成，表元数据又声称同一分区存在且可查询。`McpToolExecutor` 与
`StdioMcpClient` 的集成测试分别调用三个真实 MCP 工具，证明协议层忠实传输全部 Observation；协议层
不负责选择“更可信”的一侧，因为那会静默丢失审计事实。Golden runner 随后把三个 source ID 投影为
领域 Evidence，评分器按以下顺序处理：

```text
load golden-case:v10
  -> validate conflict sources / forbidden roots / no-root obligation
  -> replay three successful ToolEvent + Evidence objects
  -> verify every annotated conflict source was observed
  -> inspect every final report root cause for forbidden hits
  -> verify empty root causes when required
  -> verify uncertainties are disclosed when required
  -> publish per-case safe resolution and aggregate conflict metrics
```

`evidence_conflict_safe_resolution` 必须同时满足来源完整、禁止根因零命中、空根因义务和 uncertainty
义务；聚合层另外发布 `forbidden_conflict_root_hit_count`，便于区分“漏看一条冲突证据”和“看见全部
证据后仍武断下结论”。负向测试保留全部 Evidence，并让错误根因引用一个真实 evidence ID，因此普通
引用完整率仍为 1；专用安全指标必须失败，证明两项指标职责不同。当前只有一条确定性冲突案例，不能
外推为真实 LLM 的冲突识别准确率，未来新增同类案例或真实模型 runner 时必须提升契约并重新实测。

`golden-case:v10` 延续把跨组件类别从“人工约定”升级为 Schema 不变量。加载器从每个
`required_tools` 枚举值的前缀提取组件集合；`cross_component` 必须至少覆盖两个不同组件，并至少具有
一条 `required_fault_paths`。先检查组件数量、再检查路径存在性，能分别暴露“单组件改标签”和“调用
多个系统但没有因果关系”两种配额污染。普通单组件、记忆和冲突类别不受这条规则误伤。

第 13 条 `golden_cross_lts_blocked_by_bds_partition` 复用主演示的同一事实快照，但改变问题边界和必要
Action：先读取 LTS 状态与依赖拓扑，再读取 BDS 状态、日志和表分区。真实 MCP 集成测试验证五个响应
共同形成以下公开链路，Golden 路径标注则直接复用人工知识图中已经存在的节点和边：

```text
task_lts_order_report
  -> DEPENDS_ON -> task_bds_order_aggregate
  -> CONSUMES   -> dataset_ods_order_delta
```

评分时两段路径都必须存在于 `AgentState.retrieved_paths`，并分别被最终 `fault_chain.evidence_refs`
引用；只执行五个工具、只观察 source ID 或只检索路径但不写入报告都不能获得完整链路分。该案例把
跨组件配额提升到 2/10，但仍是确定性 runner，小样本满分不能外推为真实模型的跨系统因果推断能力。

第 14 条 `golden_cross_bds_blocked_by_flashsync_conflict` 把调查边界下移到 BDS→FlashSync。runner 回放
BDS 状态、日志、表信息和 FlashSync 延迟、日志、一致性六项 Observation；真实 MCP 集成测试另外验证
零吞吐、主键冲突错误码与一致性差异/积压数量相互印证。该案例要求三条图路径：BDS 任务依赖同步
任务、同步任务产出 BDS 消费的数据集，以及积压现象到主键冲突和解决方案的因果链。

三个路径标签分别计分，任一缺失都会降低该案例的 `fault_path_completeness`。这样既验证组件间任务
依赖和数据交接，也验证根因/方案路径；只引用主键冲突 Evidence 而不解释 BDS 为什么缺分区不能满分。
当前跨组件配额提升到 3/10，仍需继续增加不同事实环境，而不能仅在同一 Fixture 上无限改写查询。

第 15 条 `golden_cross_lts_blocked_by_bds_resource_exhaustion` 因此使用独立
`cross_lts_bds_resource_exhaustion.json`。六个 MCP 响应形成“正向根因证据 + 反向排除证据”：LTS
状态/日志/拓扑确认只是在等待 BDS；BDS 状态/日志确认资源饱和、spill 和 executor 丢失；表信息确认
输入分区按时到达、数据量接近基线，日志又确认倾斜不显著。只有这些 Observation 同时出现，才允许
输出 BDS 资源不足根因。

该案例复用已通过 PostgreSQL 消融验证的 `component_lts DEPENDS_ON component_bds` 路径，而没有为
单个 Fixture 添加一组尚未进入真实检索评测的新任务节点。真实 MCP 集成测试按六个独立 trace 调用
工具并检查结构化反证字段；Golden runner 再要求同一路径进入最终 fault_chain。这个取舍保持知识图
精简，同时把事实环境从“缺分区/主键冲突”扩展到“输入正常但计算资源耗尽”。

第 16 条 `golden_ambiguous_bds_missing_resource_context` 覆盖真正的零工具补参边界。用户只说 BDS
任务很慢，没有资源 ID 和时间窗；`golden-case:v10` 在加载阶段保证零 `required_tools` 只能属于
`ambiguous_or_insufficient`，并要求 path、Evidence source、allowed root 均为空。该类别还必须包含
`missing_resource_id`、`need_user_input` 或 `evidence_insufficient` 之一，防止模糊输入被标成证据充分。

确定性 runner 过去从 required tool 前缀推导组件，因此会错误拒绝合法零 Action 案例。现在普通案例
仍从实际工具收敛组件，零工具案例才回退到 Scenario 的已校验 `components`；它不会读取 Scenario 的
tool result。随后生成 `react_step=0`、空 ToolEvent/Evidence、无根因和带 uncertainties 的报告。生产
`BoundedReactLoop` 另有测试确认 `need_user_input` 不会进入 executor，因此评测替身与真实控制流语义一致。

第 17 条 `golden_ambiguous_flashsync_missing_causal_log` 覆盖“部分工具成功但因果证据缺失”。新的
`flashsync_incomplete_root_cause_evidence.json` 让 delay 与 consistency 成功返回同为 74 的积压/差异
数量，证明同步症状存在且两项观察相互一致；`flashsync.get_sync_log` 则返回不可重试 `EMPTY_RESULT`，
没有 Evidence。症状相关性不能替代日志中的因果错误码，因此 Golden allowed root 保持为空。

真实 MCP 集成测试检查两条成功 Evidence、一次失败 ToolEvent、空失败 Evidence 和 `retryable=false`；
确定性 Golden runner 仍执行三项必要 Action，并把两个成功 source ID 纳入覆盖率。最终报告必须无根因、
公开 uncertainties 并以 `evidence_insufficient` 停止。该设计展示“保留已知事实”和“克制未知因果”可
同时成立，而不是遇到一个工具失败就丢弃全部 Observation，或根据积压数量猜测主键冲突。

第 18 条 `golden_ambiguous_lts_all_observations_unavailable` 复用 `lts_empty_result.json`，但把调查范围
从单一失败日志扩展到 LTS 状态、日志和依赖拓扑。复用同一 Fixture 是刻意的：Golden Case 表达用户问题
和必要行为，Fixture 表达事实环境；不同问题可以在不复制数据的情况下选择不同工具子集。

真实 MCP 路径中，状态和日志的 `EMPTY_RESULT` 属于稳定缺数，各产生一个 `retryable=false` 事件；拓扑
`TIMEOUT` 属于瞬时失败，执行器按统一预算重试一次并保留 attempt 1/2。四个底层 ToolEvent 都保留用于
审计，但三个逻辑 Observation 的 Evidence 和 observation refs 全为空，因为错误消息只能证明查询失败，
不能证明任务成功、失败或某个依赖是根因。Golden runner 按三个逻辑 Action 计分，不把 MCP 内部重试
误算为 Planner 重复 Action；报告必须无根因、公开不确定性并以 `evidence_insufficient` 停止。

第 19 条 `golden_lts_invalid_partition_parameter_single` 使用独立
`lts_parameter_validation_failure.json`。状态工具证明配置阶段三次重试耗尽，日志工具提供
`INVALID_PARTITION_DATE`、参数名、期望格式和脱敏输入值，拓扑工具则证明两个上游均已就绪。诊断
必须把日志作为直接根因证据，同时保留拓扑作为排除“上游未就绪”的反证；支持与反对其他假设的
Evidence 都属于审计事实，不能只保留有利于 Top-1 的内容。

知识侧同步升级为 `graph-seed:v2`，新增症状、根因、解决方案三节点及 `CAUSED_BY`、`RESOLVED_BY`
两条边。PostgreSQL 测试先由可替换 Embedding Provider 写入 14 个向量，再以“LTS 参数校验失败”执行
向量/全文种子召回和两跳递归 CTE，要求返回完整有序路径。Golden 路径继续限制为最多两跳，并把
v2 source_id 投影到 RetrievedPath；这证明方案来自显式知识关系，不是 Fixture 日志硬编码的答案。

第 20 条 `golden_bds_data_skew_single` 使用独立 `bds_data_skew.json`。状态 Observation 表明 16 个
执行器在线但聚合阶段在 83% 停滞；日志提供 `DATA_SKEW_DETECTED`、9.6 倍热点分桶、27 次 spill 和
零执行器丢失；表信息提供当天分区已就绪、318 万行处于 300–340 万基线的反证。判断顺序先确认
任务确有长尾，再用日志识别分布不均，最后用表事实排除缺分区和整体输入暴增，避免把相关现象混成
同一个“任务慢”结论。

`graph-seed:v3` 为该场景增加症状、根因、方案节点以及 `CAUSED_BY`、`RESOLVED_BY` 两条边。真实
PostgreSQL 测试在 17 个节点全部生成同 Provider 向量后，用“BDS 执行阶段长尾 数据倾斜”查询执行
pgvector/全文种子召回与递归 CTE，并检查完整两跳路径。Golden runner 只把该路径投影为稳定评测输入；
实时根因仍必须引用 MCP 日志，知识库不能单独把历史通用模式升级成本次事实。

第 21 条 `golden_flashsync_checkpoint_regression_single` 使用独立
`flashsync_checkpoint_regression.json`。延迟 Observation 提供当前 offset、已提交 offset、1200 位点差
和 1200 条积压；日志提供 `CHECKPOINT_REGRESSION`、旧快照来源和自动重放保护；一致性 Observation
提供 1200 条目标缺失、零重复和相同源目标 offset 差。三项数值必须闭合，才能排除普通延迟、主键
冲突或目标端重复等候选根因。

`graph-seed:v4` 新增检查点症状、根因、方案节点以及两条因果边。PostgreSQL 测试在 20 个节点完成
同 Provider 嵌入后，以“FlashSync 检查点落后 位点回退”执行 pgvector/全文召回和两跳 CTE。路径方案
要求备份差异与检查点、核对已提交位点和幂等边界、小批量重放并复查；Golden 风险标注为 high。
项目仍只提供只读诊断，不实现自动位点修改或自动重放，避免知识建议越过 MCP 只读权限边界。

第 22 条 `golden_flashsync_schema_mapping_outdated_single` 使用独立
`flashsync_schema_mapping_outdated.json`。延迟 Observation 提供源 v12/映射 v11、600 条积压和零吞吐；
日志提供 `SCHEMA_MAPPING_OUTDATED`、未映射 `customer_tier` 和 600 条拒绝；一致性 Observation 提供
600 条解析失败、600 条目标缺失和零重复。校验顺序先确认版本差，再确认直接错误码，最后核对影响
数量闭合，避免把 Schema 漂移误判为主键冲突、检查点回退或普通延迟。

`graph-seed:v5` 新增 Schema 拒绝、映射滞后、映射验证三节点及两条因果边。PostgreSQL 在 23 个节点
完成同 Provider 嵌入后，以“FlashSync Schema 记录拒绝 字段映射滞后”执行向量/全文召回与两跳 CTE。
方案只允许比对源目标 Schema、预览字段/default 语义、小批量回放和一致性复核；项目仍不实现映射
写入或自动回放。该案例把单组件类别补齐到 8/8，后续案例只扩跨组件事实环境。

第 23 条 `golden_cross_customer_profile_schema_propagation` 使用独立
`cross_customer_profile_schema_propagation.json`，而不是把第 22 条单组件 Fixture 改标签复用。这样做的
原因是跨组件案例必须拥有自己的 LTS/BDS 事实：LTS 状态公开 600 条上游缺口，拓扑给出客户画像
LTS→BDS→FlashSync 任务身份；BDS 状态证明资源利用率正常但 5000 条输入只到 4400 条，表信息同时
证明分区已经存在且缺口仍为 600；FlashSync 日志给出源 v12、映射 v11、`customer_tier` 未映射和
600 条拒绝，一致性再确认 600 条解析失败/目标缺失且零重复。校验顺序先确认调度确实被上游阻塞，
再排除 BDS 资源与缺分区，最后用同步错误码和数量闭环确定根因，避免从用户问题直接跳到 FlashSync。

六项 Action 恰好等于默认 ReAct 工具预算，因此 Fixture 不额外要求 LTS/BDS 日志或同步延迟；当前六项
已经分别覆盖传播、排除和根因三种证据职责，继续探测不会增加 Golden 所需信息。真实 MCP 测试为每项
调用使用独立 trace，并检查六个 source ID 与 600 数量闭合，证明数据经过 stdio 协议而非仅由评测脚本
读取 JSON。`graph-seed:v6` 增加四个任务/数据集节点与八条边：三条 RUNS_ON 保留所属组件，两条
DEPENDS_ON 形成 LTS→BDS→FlashSync 两跳任务链，PRODUCES/CONSUMES 表达数据交接，MANIFESTS_AS
把同步任务接入 v5 Schema 症状。PostgreSQL 测试从 LTS 任务和 FlashSync 任务分别检索两条路径；Golden
还要求 v5 症状→根因→方案链，三条路径必须进入最终 fault_chain 才算完整。所有工具仍是只读观察，
方案不会自动修改映射、重跑任务或回放数据。

第 24 条 `golden_cross_bds_blocked_by_flashsync_checkpoint_regression` 使用独立
`cross_bds_flashsync_checkpoint_regression.json`。BDS 三项 Observation 先建立排除链：任务停在
source_validation，CPU 22%、内存 28%、倾斜 1.03，当前分区已经存在；真正异常是 8000 条预期输入
只到达 6800 条，物化 offset 87220 比期望 88420 落后 1200。FlashSync 三项 Observation 再建立直接
因果链：当前/已提交 offset 差、积压、旧检查点差、目标缺失均为 1200，日志明确
`CHECKPOINT_REGRESSION`，一致性又确认零重复。先排除 BDS 局部原因、再验证同步位点和影响数量，能
防止把任何输入缺数都归因于缺分区，也防止仅凭普通延迟就猜测检查点回退。

六项工具恰好用满默认 ReAct Action 预算；每项分别承担运行状态、反证、数据影响或直接根因职责，
不再加入没有信息增益的额外探测。真实 MCP 测试用六个独立 trace 验证稳定 source ID 和 1200 等式。
`graph-seed:v7` 增加 BDS/FlashSync 任务与客户状态数据集：RUNS_ON 记录归属，DEPENDS_ON→PRODUCES
形成 BDS 到同步数据集的两跳交付链，CONSUMES 保留数据消费事实，MANIFESTS_AS→CAUSED_BY 把同步
任务接入 v4 检查点根因。PostgreSQL 对两条新路径分别查询，Golden 还要求 v4 根因→恢复方案链进入
fault_chain。风险标注为 high，因为检查点恢复可能造成重复或遗漏；项目只输出备份、位点/幂等核对、
小批量验证和回滚提示，不实现检查点写入、自动重放或 BDS 自动重跑。

第 25 条 `golden_cross_lts_blocked_by_bds_data_skew` 使用独立
`cross_lts_bds_data_skew.json`，把单组件倾斜知识放进新的 LTS→BDS 传播环境，而不复用第 20 条 Fixture。
LTS 三项 Observation 证明报表任务等待客户分群 BDS 聚合，并给出同一任务/数据集身份；BDS 状态证明
16 个执行器仍在线、CPU 68%、内存 64%，但聚合停在 83% 达 1080 秒。BDS 日志随后给出
`DATA_SKEW_DETECTED`、热点分桶 `synthetic_segment_unknown`、9.6 倍 skew、27 次 spill 与零执行器丢失，
这是允许确定根因的直接证据；表信息确认目标分区存在且 318 万行落在 300–340 万历史基线，是排除
缺分区和整体输入暴增的反证。按照“传播事实→直接根因→反证”的顺序校验，可以避免仅凭 LTS timeout
猜测调度问题，也避免看到 spill 后错误归因于资源耗尽。

六项工具恰好使用默认 ReAct Action 上限，每项分别承担跨层身份、症状、直接根因或反证职责，因此
没有加入无信息增益的额外探测。真实 MCP 集成测试通过六个独立 trace 验证稳定 source ID、83%/1080
秒、9.6/27 和正常行数确实穿过 stdio 协议。`graph-seed:v8` 增加 LTS/BDS 任务与客户分群数据集：
RUNS_ON 记录任务归属，DEPENDS_ON→PRODUCES 形成 LTS 到 BDS 数据集的两跳交付链，CONSUMES 保留消费
事实，MANIFESTS_AS→CAUSED_BY 把 BDS 任务接入既有长尾/倾斜知识。PostgreSQL 分别从 LTS 和 BDS
任务检索两条新路径；Golden 还要求 v3 根因→再平衡方案链进入 fault_chain。风险标注 medium，系统只
输出人工检查热点键、采样分布和隔离环境验证建议，不执行 SQL 改写、扩容、自动重跑或任何生产写操作。

第 26 条 `golden_cross_lts_bds_flashsync_target_write_throttle` 使用独立
`cross_lts_bds_flashsync_target_throttle.json`。六项 Action 按三组件职责分组：LTS 状态确认本地执行
未开始且缺 2600 条，拓扑给出收入日报→BDS 收入聚合→FlashSync 支付增量身份；BDS 状态证明 CPU
27%、内存 34% 且 12000 条预期输入只到 9400 条，表信息同时确认分区存在、Schema 兼容和同一
2600 条缺口；FlashSync 延迟证明源端读取健康但吞吐从 450 降到 8 行/秒、积压 2600 条，日志再给出
连续 18 次 `TARGET_WRITE_THROTTLED`、目标写配额 100% 和自动提额被阻止。校验顺序先建立传播链，
再排除 LTS/BDS 局部故障与源端读取问题，最后用错误码和配额确认根因，避免从单一低吞吐直接猜测。

六项工具恰好使用默认 ReAct Action 上限，没有为漂亮数量闭环加入无信息增益调用。真实 MCP 测试为
每项调用使用独立 trace，并验证 LTS/BDS/FlashSync 的 2600 等式确实经过 stdio 协议。`graph-seed:v9`
增加七个节点和十条边：两条 DEPENDS_ON 形成三组件任务链，PRODUCES/CONSUMES 保留支付数据集交接，
MANIFESTS_AS→CAUSED_BY 连接同步任务与目标限流根因，CAUSED_BY→RESOLVED_BY 再连接只读受控方案。
PostgreSQL 用三个查询分别验证这三条两跳路径。medium 风险方案只建议人工核对配额、评估临时提额
或限速分批恢复、核对差异和回滚边界；项目不实现自动改配额、自动回放或生产目标写入。

第 27 条 `golden_cross_lts_bds_flashsync_source_authorization_expired` 使用独立
`cross_lts_bds_flashsync_source_auth_expired.json`。六项 Action 同样按三组件分工：LTS 状态确认本地
计算未开始且缺 1800 条，拓扑给出结算摘要→BDS 结算聚合→FlashSync 结算增量身份；BDS 状态证明
CPU 25%、内存 29% 且 10000 条预期输入只到 8200 条，表信息再确认分区存在、Schema 兼容和同一
1800 条缺口；FlashSync 延迟明确目标写入健康、源端读取异常、吞吐为零和积压 1800 条，日志以
`SOURCE_AUTHORIZATION_EXPIRED` 和 1800 条读取拒绝确认源端授权租约过期。校验先建立传播和数量闭环，
再用 BDS 与目标端健康事实排除局部原因，最后才接受直接业务错误码，避免把任何零吞吐都猜成授权问题。

六个 MCP 响应均为 `ok=true`，只表示 JSON-RPC/工具协议成功返回；业务字段仍可表达失败，这一分层
防止执行器错误地重试确定性授权故障。Fixture 只保存合成租约标识
`synthetic_lease_settlement_reader_v3` 与 `authorization_value_exposed=false`，不保存、生成或展示
令牌、口令及任何授权值。真实 MCP 测试使用六个独立 trace 验证 1800 等式、源/目标健康方向和安全
标志确实穿过 stdio。`graph-seed:v10` 新增七个节点和十条边，分别形成三组件依赖链、同步任务到授权
拒绝根因链和根因到安全轮换方案链；PostgreSQL 用三个查询验证路径。high 风险方案只允许受控渠道
轮换、最小权限验证、小批量恢复与旧租约撤销，不授权自动改密、自动重放或生产系统写入。

第 28 条 `golden_cross_lts_bds_flashsync_watermark_timezone_mismatch` 使用独立
`cross_lts_bds_flashsync_watermark_timezone_mismatch.json`。LTS 状态先确认订单履约日报被数据质量门禁
阻止、本地计算未开始且缺 900 条，拓扑给出履约日报→BDS 履约聚合→FlashSync 订单事件任务身份；
BDS 状态证明 CPU 31%、内存 36% 且 7200 条预期输入只到 6300 条，表信息再确认分区存在、Schema
兼容和同一 900 条缺口。FlashSync 日志明确源事件时间为 UTC、水位线按 Asia/Shanghai 解释、偏移
480 分钟，并返回 `WATERMARK_TIMEZONE_MISMATCH` 与 900 条跳过记录；一致性抽检则确认源端 7200、
目标端 6300、缺失 900 且零重复。校验按“传播→局部反证→错误码→一致性闭环”执行，防止把
completed_with_quality_error 当成数据正确，也避免把漏数误报成缺分区、Schema 漂移或重复写入。

六项工具恰好使用默认 ReAct Action 上限。真实 MCP 测试用独立 trace 证明 LTS/BDS/日志/一致性的
900 等式确实穿过 stdio，而非 Golden runner 直接读取 Fixture 冒充协议。`graph-seed:v12` 新增七个
节点和十条边，分别形成三组件任务依赖、同步任务→静默漏数症状→水位线时区根因，以及根因→受控
回补方案；PostgreSQL 从任务和症状查询三条完整有向路径。风险标注 high，因为改水位线和回补可能
造成重复、越界或二次漏数；项目只建议冻结位点、核对时间语义、隔离校准、小批量回补、幂等/
一致性检查和回滚点，不实现自动修改水位线、自动回补或生产写入。

v9 新知识与既有 `sync backlog` 消融查询存在合理语义重叠，因此 Evidence Bundle 的候选排序发生
变化。固定 Provider 和默认预算下实测从 v8 的 5881 字节/8 节点变为 5634 字节/7 节点，仍选择 4 条
完整路径，并公开省略 5 个节点和 4 条路径；主键冲突必要路径仍完整，消融指标没有被虚增。测试锁定
这些数字，是为了要求知识扩展后重新解释上下文组成，而不是把旧快照当成与数据无关的常量。
v10 结算授权知识没有进入该固定查询的最终候选，PostgreSQL 重跑后仍为 5634 字节/7 节点/4 路径、
省略 5 个节点和 4 条路径；文档保留相同快照是重新验证后的结果，不是因为版本升级而假定结果不变。
`document-retrieval:v1` 让计费主体多出 `,"selected_documents":[]` 这 24 字节的规范包装键，因此当时
实测快照为 5658 字节/7 节点/4 路径、省略 5 个节点和 4 条路径；图证据的选择逐条未变，变化只来自
共享字节预算新增了第三类证据入口。`graph-seed:v12` 给每个紧凑节点加上 `remediation_risk_level`
（6 个事实节点各 31 字节的 `null`、1 个方案节点 32 字节的 `"medium"`），当前实测快照因此为
5876 字节/7 节点/4 路径、省略 5 个节点和 4 条路径。空值同样进入 Prompt，所以它必须照实计费——
在字节统计里排除 `null` 只会让预算账目好看而 Planner 上下文照旧变大。
v11 订单水位线知识加入后再次重跑仍保持相同 Bundle 数字和主键冲突完整路径；新检索断言使用
`dws_order_fulfillment_daily`、`flashsync_order_event_delta` 与“FlashSync 增量窗口静默漏数”，证明
新知识真实入库，同时不把它包装成固定消融查询的额外收益。

`required_fault_paths` 每项包含唯一 `path_label`、2–3 个有序 node ID 和恰好
N-1 个白名单关系类型，匹配当前 GraphRAG 1–2 跳预算。链路评分只查看同时满足两个条件的路径：
一是存在于 `AgentState.retrieved_paths`，二是其 path_id 被最终 `DiagnosisReport.fault_chain` 引用。
节点和关系分别按最长有序子序列计算覆盖并取较小值，避免节点碰巧出现、关系错误或未使用候选得分。

当前 manifest 是四层小样本消融加一层 28 条 Golden 确定性回归。Golden 报告固定
`target_case_count=28`、`case_coverage_rate=28/28` 和 `target_coverage_complete=true`，表示案例数量与
五类配额达到产品基线；确定性 runner 按标注选择 Action 和根因，所以满分不代表模型级
意图识别、Top-1 根因或 P95 成绩。详细条件和实测见 `docs/golden-diagnosis-eval-results.md`。

PostgreSQL 测试使用 `postgres` marker。普通 `pytest` 默认排除它，保持无 Docker 环境下的快速反馈；显式数据库验证使用：

```powershell
$env:DATAOPS_TEST_DATABASE_URL='postgresql+asyncpg://...'
python -m pytest -m postgres
```

### 16.5 真实模型 Golden 冒烟评测与安全调用遥测

确定性 Golden runner 的职责是验证数据集、评分器和安全门禁，不是测量 Planner/Auditor 模型质量。
为了让真实模型可以复用完全相同的评分逻辑，`app/evaluation/live_golden.py` 实现独立
`live-golden-eval:v3`：它进入 FastAPI lifespan，取得生产 `DiagnosisApplicationRuntime` 与已验证
Fixture registry，为每条案例创建独立 PostgreSQL session，再顺序运行 GraphRAG、双 Agent、真实
stdio MCP、Auditor 和 memory staging。它没有加入默认 `portfolio-eval-manifest:v24`，因为离线 CI
没有密钥时应明确不运行，而不是让完整 Portfolio 永久 blocked 或偷偷换回确定性替身。

默认仍固定三条低成本代表案例：LTS 参数错误单组件、订单水位线时区错配三组件链路，以及 BDS 三个
成功响应事实冲突。这个选择同时覆盖直接根因、跨层传播和“必须克制下结论”的安全边界，但分母只有
3，不能外推为 28 条真实模型成绩。

v1 到 v2 的唯一变化是把样本口径从两档扩成三档：`scope` 现在是 `smoke` / `full` / `custom`，由
`resolve_live_golden_scope` 判定，`--all-cases` 按 Golden 文件声明顺序展开全部 28 条。这样做是因为
v1 只有 `smoke` 与 `custom` 两个值，全量运行会和“我从 28 条里挑了 5 条”共用同一个 `custom` 标签，
读者无法从报告本身分辨分母。判定规则是不对称的：`smoke` 用序列比较，保证多轮之间逐案可比；
`full` 用集合比较，因为“是否覆盖全集”与执行顺序无关；少一条立即退回 `custom`，不允许“接近全集”
被读成全量。`--all-cases` 与显式 `--case-id` 互斥并在产生模型费用前失败，避免报告 scope 与实际
分母不一致。全量运行是付费路径，因此仍然必须显式请求，默认命令不会替使用者花掉 28 条的调用。

v2 到 v3 增加的是历史预置，而不是任何评分规则。`app/evaluation/live_history_seed.py` 由
`--seed-history` 显式打开，在计时和第一次付费聊天调用之前把 Golden 记忆类案例的 `required_memories`
写成 confirmed 案例、把 `forbidden_memory_ids` 写成 pending 与 rejected 案例，报告里新增
`history_seed` 字段公开这三组 ID 与向量空间。它解决的是一个被 Run H 暴露的口径缺陷：确定性 runner
由脚本替身直接合成 `CaseMemoryMatch`，而 live 模式走生产 confirmed-only 检索路径，数据库里没有
confirmed 案例时 `history_recall_coverage`、`confirmed_only_recall_rate`、
`history_projection_pass_rate`、`realtime_priority_rate` 四个指标必然是 0——那是"没测"，不是"模型
没做到"，而报告此前无法区分这两件事。

预置刻意受三条约束。第一，内容只来自案例的用户问题与 `history_expectation` 标注，不写入
`allowed_root_causes`、`required_tools`、必要证据来源或停止原因；但 `historical_root_cause` 本身是
标注的一部分，非冲突案例的历史根因与本次正确根因相同，所以开了预置的记忆类案例根因指标与未开预置
的历史运行**不可同列比较**，`history_seed` 字段的存在就是为了让这件事无法被忽略。第二，confirmed
只能由 `PostgresMemoryRuntime.decide(CONFIRM)` 产生，与用户在 `/demo` 点确认完全同一个事务，因此
动态 case 图节点与 `SIMILAR_TO` 边一并建立，图通道召回不是未经验证的假设。第三，forbidden 记忆的
向量取自同一批案例问题而不是随机文本：只有和查询足够相似，"非 confirmed 不得被召回"才是真被测过的
门禁，而不是因为向量不相关自动成立。顺序固定为"先按 ID 删除旧行 → 单事务批量插入 pending → 逐条
决策"，重复运行因此幂等，任何一步失败都整批抛错终止评测，绝不降级成一轮记忆指标为 0 的付费运行。

这个机制已经执行过一轮（Run I，3 条记忆案例、`scope=custom`）：`history_recall_coverage` 与
`confirmed_only_recall_rate` 实测 1.0000，`history_projection_pass_rate` 与
`realtime_priority_pass_rate` 实测 0.0000。后两项的 0 是真实的但成因不在历史处理：三条案例的报告都在
Auditor 两轮 `revise` 后 `safe_degraded`，而降级报告按设计清空 `root_causes` 与 `similar_cases`，因此这
两个指标同时在测"报告是否被放行"。这是预置补上分母之后才暴露出来的口径缺陷，完整口径见
`docs/live-golden-eval-results.md` 的 "Run I" 与"仍未达标"两节。

Live runner 必须解决一个只存在于 Mock 环境的路由问题：MCP 按 `scenario_id + tool_name + resource_id`
精确查找 Fixture，而普通用户问题不一定包含所有机器资源 ID。`build_live_golden_message()` 因此只从
Scenario 读取 scenario ID、资源 ID、时间窗口和组件，并把它们作为明确标记的合成路由元数据追加到
不可信 user 消息。它刻意不读取或渲染 Golden 的 `required_tools`、allowed roots、required evidence
sources、fault paths、stop reasons 和 risk answer。测试逐一搜索这些字段，保证模型必须通过 Action /
Observation 得到事实，不能从评测答案抄写报告。

模型调用可观测性位于 `app/observability/model_calls.py`，核心不是全局日志，而是以下作用域：

```text
CLI creates InMemoryModelCallRecorder
  -> ContextVar binds recorder to current asyncio task
  -> Planner/Auditor Provider starts ModelCallMeasurement
  -> official SDK parse returns or raises a stable domain branch
  -> measurement records role/version/status/duration/optional usage
  -> CLI finally resets the ContextVar token
  -> live-golden-eval:v3 aggregates calls and existing Golden report
```

选择 `ContextVar` 而不是在 Provider 保存 `last_usage`，是因为 Planner/Auditor 实例会被 FastAPI 并发
请求复用：共享最后值可能把 A 请求 token 记到 B 请求。Measurement 在请求开始时捕获当前 recorder，
使用 `perf_counter()` 单调计时，并用 `_finished` 阻止同一调用被成功与异常分支重复记录。没有绑定时
`finish()` 只关闭本次测量状态，不追加任何列表，所以普通 API 请求不会造成无界内存增长。

`model-call-metric:v1` 的 Pydantic Schema 没有文本载荷字段，只包含 Planner/Auditor 角色、Provider /
Prompt 契约、模型名、成功/结构失败/拒绝/超时/连接/HTTP 状态、耗时和可选 token。usage 只通过属性
读取 prompt/completion/total 三个整数，不序列化完整 SDK response；兼容端点漏报 usage 时单独增加
`unreported_usage_call_count`，不能把未知成本写成零。Prompt、修复前原始输出、refusal 正文、base URL、
API key、Thought 和 traceback 都不会进入 recorder 或实测报告。

命令在任何付费调用前检查 `DATAOPS_CHAT_PROVIDER`、本地 SecretStr key 和数据库 URL，code revision
必须显式传入：

```powershell
$env:DATAOPS_DATABASE_URL='postgresql+asyncpg://...'
$env:DATAOPS_CHAT_PROVIDER='openai-compatible'
$env:DATAOPS_CHAT_API_KEY='仅在本机环境设置'
.venv\Scripts\python -m app.evaluation.live_golden `
  --code-revision '<git commit>' `
  --output 'live-golden-smoke.json'
```

当前仓库只完成可执行基础和 MockTransport/路由隔离测试，没有保存真实模型密钥，也没有发布测量
数字。`docs/live-golden-eval-results.md` 明确记录这一状态；只有上述命令在固定模型、Prompt、数据和
代码版本下成功生成 `metric_kind=measured` 报告后，才能新增真实成绩，不得用 15-token 合成 SDK
响应或确定性 Golden 满分填表。

### 16.6 必需单页 Demo 的实现顺序

产品 M4 的单页前端已经是完成定义的一部分。资源 API 使用 PostgreSQL Worker、
queued/running/completed/failed/cancelled 状态和轮询语义；页面不把 POST 响应误当作最终结果，
而是按 run_id 读取服务端快照与事件。

已固定的实现选择是由 FastAPI 同容器静态托管原生 HTML/CSS/模块 JavaScript：它复用现有结构化 API，
避免为单页演示引入 Node 服务、CORS 和重型状态库。页面必须展示健康状态、session/input、run 轮询、
Action/Observation 公开时间线、Evidence、GraphRAG 路径、已审计报告、风险、不确定性和 memory
confirm/reject；不得展示 Thought、Prompt、Provider 响应或凭据。所有 JavaScript callable 也必须有
详细 JSDoc，关键轮询/AbortController/事件去重步骤解释设计原因与失败语义。

完整信息架构、状态机、安全边界、响应式/可访问性和验收条件见 `docs/frontend-design.md`；
静态托管、前端集成测试和 Docker 同源加载已完成，浏览器截图属于后续作品集展示材料而非运行时缺口。

## 17. 配置与生成文件说明

| 文件 | 为什么不逐行注释 | 如何理解和验证 |
|---|---|---|
| `requirements*.lock` | 由 pip-tools 机械生成，手工注释会在再生成时丢失。 | 依赖来源在 `pyproject.toml`，一致性由 `pip check` 和 Docker 构建验证。 |
| `data/fixtures/**/*.json` | 标准 JSON 不允许注释。 | Pydantic Scenario Schema 和 Fixture 测试。 |
| `data/knowledge/*.json` | 需要被标准加载器和其他语言读取。 | `KnowledgeSeedBundle`、source_span 校验和 PostgreSQL 集成测试。 |
| `data/evals/*.json` | 标准评测数据不能加入非标准注释。 | 评测 Schema、五层 Portfolio Manifest、快速加载测试、本文档和对应实测报告。 |
| PNG / DOCX | 二进制格式不能可靠保存代码式注释。 | Markdown 产品基线、本文档和正式阅读版正文。 |

## 18. 当前完成度与下一步

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
- Planner v4 双消息 Prompt、会话上下文、确定性历史解释、OpenAI-compatible Structured Outputs Provider 与一次 Schema 修复。
- 确定性报告草稿、引用/风险门禁、独立 Auditor Structured Outputs 与最多一次报告级返工。
- `case-memory:v2` 受控长期案例、向量/图融合召回、检索通道/分量/edge 引用、同 run 幂等和 confirmed-only API。
- `memory-recall-eval:v1` 六条长期记忆召回案例、双模式消融、Recall/Precision/forbidden 指标和 PostgreSQL 实测报告。
- `history-impact-eval:v1` 三条 Memory off/on 诊断案例、实际 ToolEvent 行为指标、实时事实优先门禁和真实 LangGraph 实测报告。
- `auditor-impact-eval:v1` 三条语义缺陷案例、规则/Auditor 增量归因、危险残留与安全处置指标和真实报告 LangGraph 实测。
- `golden-case:v10` Schema/位点/倾斜/参数/限流/授权/水位线反证、补参/降级/跨组件门禁、记忆与冲突标注、十四条案例的知识图根因锚点，以及 `golden-diagnosis-eval:v23` 二十八条案例评测。
- `portfolio-eval-manifest:v24` 五层受限 pytest 入口、v1–v23 兼容读取、指标发布门禁，以及 `portfolio-eval-run:v23` 单命令 JSON 汇总。
- `live-golden-eval:v3` 生产路径真实模型入口、smoke/full/custom 三档样本口径、`--seed-history` 历史预置与 `history_seed` 分母声明、Golden 答案隔离、ContextVar 安全遥测和 measured-only 报告契约。
- `audited-diagnosis-workflow:v2` 按需召回、两阶段案例解释、ReAct、Auditor 和审计后 staging 顶层闭环。
- `diagnosis-resources:v4` session/message/run/event PostgreSQL 资源 API、取消/恢复、完整相似案例结果和安全失败事件。
- `session-checkpoint:v1` 同 session 成功快照、追问恢复、版本门禁、失败保护和跨 run Action 去重。
- `document-retrieval:v1` Runbook/SOP/复盘/FAQ 第二条知识通道、结构感知切片、pgvector + 全文混合召回和 cross-encoder 精排。
- `run-trace:v1` 七层 per-run 调用链、ContextVar 父指针推导、同事务落库和 `GET /api/v1/runs/{run_id}/trace` 回放。
- `runtime-metrics:v1` 数据库侧聚合的五组 Prometheus 指标族与 `GET /metrics` 曝光，runtime 未装配时返回 503 而非全零。
- `api-auth:v1` 前缀 fail-closed 的中间件鉴权、按来源 IP 滑动窗口限流、限流先于鉴权的判定顺序、逐字相同的 401 与两个方向的半配置启动拒绝。
- `run-stream:v1` 只读 SSE 增量推流、三种命名帧、run 级/连接级终止原因分类、先读快照后读事件的顺序不变量、标准 `Last-Event-ID` 续传与永久保留的轮询回退。
- 双 Agent 契约的四级降级阶梯、规则对模型的非对称否决权、工作流侧返工预算、Auditor 不可用即降级，以及 run 结果/`run_events`/trace/`/demo` 裁决卡四档暴露粒度。

明确保留的模型接入项：

- 模型级 Embedding Provider（当前默认实现是离线 feature hashing 基线）。
- LangGraph 框架内部逐节点中断恢复、模型级复杂历史语义对比，以及固定真实模型/Prompt 的 28 条端到端评测快照。

已完成的非模型闭环：

- PostgreSQL Worker 的 queue/lease/heartbeat/有限重试、cancelled 终态和 run-level checkpoint 恢复。
- 超长会话 checkpoint 的有界滚动窗口、案例永久删除 API、前端 Demo 及其静态托管/安全测试。
### diagnosis-resources:v4：PostgreSQL Worker、取消与恢复

本切片把 message API 固化为可靠异步资源契约：POST `/api/v1/sessions/{session_id}/messages` 只在短事务中创建 `queued` run，并返回 HTTP 202；客户端通过 GET `/api/v1/runs/{run_id}` 轮询 `queued -> running -> completed|failed|cancelled`，GET events 读取公开时间线。`POST /api/v1/runs/{run_id}/cancel` 以行锁原子写入 `user_cancelled` 与 system event，`POST /api/v1/runs/{run_id}/resume` 只接受 cancelled 来源并创建新 run；浏览器断开请求不会伪造服务端状态。

Worker 不引入 Redis/Kafka，也不创建第三个 Agent。它使用 PostgreSQL `FOR UPDATE SKIP LOCKED` 领取最早任务，在事务内递增 `attempt_count`、写入 `lease_owner/lease_expires_at`，提交后才运行 GraphRAG、Planner、Auditor、MCP 和 memory。心跳以条件 UPDATE 延长租约；进程崩溃或超时后，其他 Worker 只会在租约过期且未达到最大次数时接管，达到上限则写入安全 `worker_attempts_exhausted` system event。完成、失败、事件和 checkpoint 仍然在一个事务内提交，因此不会出现“状态 completed 但时间线/上下文缺失”。

数据库同时使用 `(status, created_at)` 队列索引和 `session_id WHERE status IN ('queued','running')` 部分唯一索引：前者让领取按 FIFO 近似有界扫描，后者把同一 session 的并发追问转成 HTTP 409，而不是让两个 workflow 竞争同一个 checkpoint。旧版本遗留的 running 行在 Alembic `20260716_0006` 中安全标记为 failed；`20260716_0007` 再扩展 cancelled 约束，因为取消必须保留可验证的用户意图和完成时间。

学习型验证顺序是：先运行 `ruff` 与非 PostgreSQL 单元/路由测试，再运行真实 PostgreSQL 迁移和 Worker 集成测试，最后通过 Docker `/health` 检查 `execution_mode=postgres-worker`、Worker 参数和契约版本。所有测试数据仍为合成/Mock，不接入生产系统。
### 16.7 单页 Demo 与记忆决策闭环

前端切片采用 FastAPI `FileResponse` 同源托管 `app/static/demo/index.html`、`styles.css` 与 `app.js`。这种选择让求职演示只需要启动一个服务，同时保留浏览器端可检查的原生 HTTP、DOM 安全和状态机实现。`app.js` 将后端 `AgentRunSnapshot` 映射为可见状态，并以递增退避轮询 run/events；浏览器刷新后仍可用 run ID 重新获取状态，不把内存 spinner 当作任务真相。

报告完成后，页面读取 `DiagnosisRunResult.memory_stage.memory`。候选的 `memory_id/root_cause/components/status` 是不含 embedding 的领域投影；pending/rejected 时显示 confirm/reject/delete。按钮分别调用有限枚举决策或 `DELETE /api/v1/memories/{memory_id}`，再使用服务端返回结果更新 UI。这样避免前端自行修改状态，也保持“只有显式用户确认才进入 confirmed recall”的长期记忆原则。失败路径保留候选并显示错误，避免把网络失败误报为已确认或已删除。

HTML/CSS 负责信息结构和可访问布局，JavaScript callable 均有 JSDoc，复杂状态转换旁有解释性注释；测试验证 `/demo`、CSS、ES module、path traversal 404、409 active run 和 memory decision endpoint。Docker 健康检查还验证 `/demo` 与 `/demo/static/app.js` 可被容器同源加载。

## 19. 持久化 per-run 调用链与运行时指标

### 19.1 为什么进程内 recorder 不够

第 16.5 节的 `model-call-metric:v1` 只回答“这次评测一共花了多少 token”。生产多 Agent 系统还需要
回答另一个问题：**这一次 run 的 30 秒具体花在哪一层**。`app/observability/model_calls.py` 的
`ContextVar` recorder 不落库、不导出、进程级，而真实执行发生在 PostgreSQL Worker 里——Worker 重启、
多进程部署，或用户第二天回来查看历史 run 时，进程内指标已经不存在。因此本切片新增 `run-trace:v1`：
span 与 run 终态写在同一事务，并通过 `GET /api/v1/runs/{run_id}/trace` 回放。

两者不是替代关系，而是两个出口：recorder 服务离线评测的成本聚合，span 服务单次 run 的时间轴。
`ModelCallMeasurement.finish()` 因此同时写两边——即使没有绑定 recorder（普通 API 请求），也会落一条
`model.chat_completion` span，否则生产 trace 会缺掉整个模型层。

### 19.2 span 契约与安全约束

`app/observability/tracing.py` 定义 `TraceSpan` 与 `RunTrace`：

- `run_id` 本身就是 trace 标识，不引入第二套 trace ID 体系。
- `span_id` 由 `sha256(f"{run_id}|{sequence}")[:16]` 派生为 `span_*`，与 `run_evt_*` 同一套确定性
  规则，因此 Golden 回放可以逐字比对；随机 UUID 会让同一次 run 每次导出产生不同引用。
- `duration_ms` 取 `perf_counter()` 单调差值，墙钟时间戳只用于时间轴展示，系统时间回拨不会产生
  负耗时。
- `TraceSpanKind` 固定为 `workflow / node / react_step / tool_call / retrieval / model_call /
  persistence` 七层，与三层嵌套 LangGraph、MCP 协议边界和两条检索通道一一对应；自由字符串会让
  同一段耗时在不同 run 里落进不同类别，事后无法比较。
- `TraceSpanStatus` 只有 `ok / error / cancelled`。取消是外部中断而不是缺陷，与错误混为一谈会让
  错误率随用户取消行为波动。异常类型与异常消息刻意不进入遥测。

“绝不外泄推理过程”在这里落实为结构性约束而不是评审纪律：span 名称必须匹配
`^[a-z][a-z0-9_.]{2,63}$`，属性键必须匹配 `^[a-z][a-z0-9_.]{1,39}$`，属性字符串值必须整体匹配
`^[A-Za-z0-9_.:\-/+]+$` 且不超过 120 字符，单个 span 最多 12 条属性。空格与 CJK 被排除，因此一句
Prompt、Thought、日志原文或 Provider 原始响应**在类型层面就无法**写进 trace。`None` 保留为“显式
未知”，不用 0 冒充“没测到”。

### 19.3 采集器：ContextVar 父指针与序号压实

`RunTraceCollector` 在 `DiagnosisApplicationRuntime` 的执行体外层绑定一次，所有插桩点通过
`ContextVar` 找到它，因此检索、MCP 与 Agent 模块不必互相传递 span 参数：

```text
runtime binds RunTraceCollector(run_id)
  -> trace_span(...) reads _CURRENT_COLLECTOR / _CURRENT_PARENT
  -> collector.open_span reserves a sequence at START
  -> span body runs; annotate()/mark() may attach safe facts
  -> _finish freezes an immutable TraceSpan into dict[sequence]
  -> collector.snapshot() compacts gaps and rebuilds parent pointers
  -> complete_run/fail_run persists spans inside the terminal-state transaction
```

两个刻意的设计选择：

1. **父指针放在 `ContextVar` 而不是采集器内部的栈。** `asyncio.gather` 会为每个协程复制上下文，
   因此并行工具调用天然共享同一个父 span；共享栈会被并发的 push/pop 互相破坏，把两个并发调用
   伪造成一条不存在的串行依赖链。第 10.4 节的并行 Action 批次正是依赖这个性质才无需额外加锁。
2. **序号在 span 开始时分配，记录按完成顺序写入字典。** 并发 span 的完成顺序与开始顺序不同，
   直接 `append` 会让父 span 排在子 span 之后，违反“父先于子”契约。被强制中断的 span 会留下序号
   空洞，`snapshot()` 在导出阶段压实：保留开始顺序、重新派生 `span_id`、用旧新映射修正父指针。
   正常路径下该映射是恒等的，压实不产生任何差异。

`MAX_SPANS_PER_RUN = 512` 是失控保护而非正常上界；超限时丢弃并把数量公开为 `dropped_span_count`，
让残缺 trace 自我暴露，而不是让一次失控的诊断把无界数据写进数据库。

### 19.4 插桩点覆盖与两种写入方式

节点级插桩用装饰器在 `graph.add_node(...)` 注册处包一层（`traced_node`），既不必给九个节点体缩进
`with` 块，也不让节点逻辑与遥测耦合。需要写属性的位置直接用 `trace_span`：

| 层级 | span 名称 | 关键属性 |
|---|---|---|
| `workflow` | `diagnosis.run`（根）、`workflow.audited_diagnosis` | `intent`、`history_trigger`、`attempt`、`resumed`、`stop_reason`、`audit_status`、`react_steps`、`tool_events` |
| `node` | `diagnosis.recall_memories`、`diagnosis.run_react`、`diagnosis.explain_matches`、`diagnosis.run_report`、`diagnosis.stage_memory`、`report.draft`、`report.audit`、`report.revise`、`report.degrade` | —（由 `traced_node` 自动包裹） |
| `react_step` | `react.planner_decision` | `react_step`、`remaining_time_ms`、`decision_status`、`action_count`、`evidence_ref_count` |
| `tool_call` | `react.tool_batch`（每轮一批）、`react.tool_call`（批内每个 Action，含执行器内部重试）、`mcp.tool_attempt`（每次尝试一条） | `action_count`、`tool_names`、`ok_count`、`failed_count`、`tool_name`、`ok`、`error_code`、`attempt`、`attempt_count`、`evidence_count` |
| `retrieval` | `retrieval.evidence_bundle`、`retrieval.graph_channel`、`retrieval.document_channel` | `retrieval_mode`、`candidate_count`、`chunk_count`、`used_bytes`、`reranker_model` |
| `model_call` | `auditor.review`、`model.chat_completion`、`embedding.embed_batch`、`reranker.rerank` | `role`、`model`、`prompt_contract_id`、`call_status`、`input_tokens`、`output_tokens`、`batch_size` |

`persistence` 层级已在契约里声明但当前没有插桩点：数据库写入都在短事务里、不构成用户等待的主要
成分，先声明层级可以让后续补插桩时不必升版本，同时避免现在制造无人查看的 span。

`mcp.tool_attempt` 单独嵌在 `react.tool_call` 之下，因为“第一次超时、第二次成功”是可靠性分析的
关键事实；只给整个 Action 一个 span 会把重试延迟平均掉而无法归因。`react.planner_decision` 与
`auditor.review` 都只包住模型往返，确定性门禁与规则校验留在 span 之外，否则两个 Agent 的延迟会看
起来比实际更高。两条检索通道分开计时，才能回答“文档 RAG 与图召回哪一条值得继续投入预算”。

`record_completed_span()` 是第二种写入方式，专供“开始与结束分散在不同方法”的既有计时器：
`ModelCallMeasurement.finish` 有多个分支，为遥测重构这段可靠性关键路径不划算，因此它接受外部测得的
`duration_ms`，开始时间由结束时间回推、仅用于展示。父指针同样取自 `ContextVar`，桥接进来的 span 仍
挂在正确的 Agent 节点下，而不是在 trace 里平铺成一层与调用关系不符的模型调用。

未绑定采集器时 `trace_span` 与 `record_completed_span` 都是零成本 no-op，所以插桩可以无条件写；
如果 no-op 路径会抛错，每个插桩点就都要写分支判断，最终一定有人漏写。

### 19.5 落库、表级约束与 `/metrics`

Alembic `20260716_0009` 创建 `run_trace_spans`：主键 `span_id`，`run_id` 外键 `ON DELETE CASCADE`
（没有 run 的 span 树无法解释，留着只会让表无界增长），`UNIQUE(run_id, sequence)`，以及复刻遥测契约的
表级 CheckConstraint——`kind` / `status` 白名单、`sequence >= 1`、`duration_ms >= 0`、
`ended_at >= started_at`、`parent_span_id <> span_id`。绕过应用层的手工写入同样无法产生负耗时、未知
层级或自引用父指针的残树。`parent_span_id` 刻意不加自引用外键：span 按开始顺序落库，父 span 通常晚于
子 span 结束，同一批 `INSERT` 内的顺序无法保证满足外键。

`complete_run` / `fail_run` 接受可选 `trace` 参数，与 run 终态、事件、checkpoint 共用同一事务：要么
都可见，要么都不可见，不会出现“run 成功但 trace 缺失”这种事后无法解释的状态。失败 run 的 trace 价值
最高——它是唯一能说明“失败前已经走到哪一层”的结构化证据。仓储在写入前校验 `trace.run_id`，因为外键
只能证明 run 存在，无法阻止把 A run 的 span 写到 B run 名下。

`runtime-metrics:v1`（`app/observability/metrics.py`）把两条 `GROUP BY` 聚合渲染成 Prometheus 文本
曝光格式，通过 `GET /metrics` 暴露五个指标族：`dataops_runs_total{status}`、
`dataops_span_count{kind,name}`、`dataops_span_error_count{kind,name}`、
`dataops_span_duration_ms_sum{kind,name}`、`dataops_span_duration_ms_max{kind,name}`。

- **聚合放在数据库而不是进程内计数器。** API 与 Worker 是不同进程且都会重启，进程内计数器归零会在
  监控面板上伪造“错误率突然下降”这一最危险的假象。按 `kind` + `name` 分组正好命中
  `ix_run_trace_spans_kind_name`，抓取成本不随 span 总量线性劣化。
- **不依赖 `prometheus_client`。** 需要暴露的只有几个计数器和一个 max gauge，而全局注册表在反复
  进入 lifespan 的测试里会出现重复注册错误。
- 标签值在 Pydantic 校验期就被限制为 `^[a-z][a-z0-9_.]{1,63}$`，因此渲染函数无需转义；非法标签会让
  抓取端丢弃**整个 job** 的全部指标，故障范围远大于拒绝单条记录。
- 耗时单位写进指标名（`_ms`），避免与 Prometheus 生态默认的秒制静默混用；空快照仍输出全部五组
  HELP/TYPE 声明，让新部署实例表现为“尚无样本”而不是“指标消失”。
- runtime 未装配时 `/metrics` 与 trace 路由都返回 503，而不是全零曝光：全零会被看板渲染成“零错误”，
  把“没部署”伪装成“很健康”。

### 19.6 验证方式

```powershell
.venv\Scripts\python -m pytest -q tests/unit/test_run_tracing.py tests/unit/test_runtime_metrics.py
.venv\Scripts\python -m pytest -q tests/integration/test_diagnosis_api.py
$env:DATAOPS_TEST_DATABASE_URL='postgresql+asyncpg://...'
.venv\Scripts\python -m pytest -m postgres tests/integration/test_run_trace_postgres.py
```

单元测试覆盖 `span_id` 确定性与序号从 1 起、唯一根与父指针推导、`asyncio.gather` 并发子 span 共享
同一父 span、序号空洞压实、超上限截断并公开 `dropped_span_count`、error/cancelled 分类与原样重抛、
`mark()` 记录“未抛异常但已降级”，以及 CJK/空格/超长属性在写入时即被拒绝。`/metrics` 的曝光文本
逐字比对：顺序不稳定会让两次抓取的 diff 充满噪声，缺少末行换行会让部分抓取端丢弃最后一个样本。

PostgreSQL 集成测试（`tests/integration/test_run_trace_postgres.py`）验证 span 与 failed 终态同事务
落库、带时区读回、JSONB 属性往返、六条表级约束真的拦住绕过 ORM 的原始 SQL 写入、`aggregate_metrics`
的错误数与 span 明细一致，以及 run 删除后 span 被级联清理。API 层测试则只验证 200/404/503 映射与
Content-Type，不重复断言聚合正确性——混在一起会让 HTTP 测试在 SQL 变更时误报失败。

契约版本 `run-trace:v1` 同时写在 `app/core/settings.py` 的 `run_trace_contract_id`、
`app/observability/tracing.py` 的模块常量、`/health` 的 `contracts.run_trace` 与 lifespan 断言里，
任一处不一致都会拒绝启动。`runtime-metrics:v1` 由 `RuntimeMetricsSnapshot.contract_id` 固定为
`Literal`，随响应模型一起校验。

## 20. 资源 API 鉴权、限流与降级边界

### 20.1 为什么这不是"以后再加的加固项"

这个服务的每个 `POST /api/v1/sessions/{id}/messages` 都可能触发 Planner、Auditor、Embedding 和
Reranker 四类付费调用，加上一次 GraphRAG 检索与九个 MCP 工具的子进程往返。一个无鉴权、无配额的
公开端口不是"演示方便"，而是把账单和数据库连接池交给任何扫描器。因此鉴权与限流和 tracing 一样属于
运行时契约，而不是部署说明里的一句建议。

同时要承认边界：本项目只需要"这个实例是否允许被调用"，不需要账号、角色和多租户。因此实现是单一
共享 Bearer 令牌 + 按来源 IP 的进程内滑动窗口，不引入用户表、JWT 签发、OAuth 或 Redis 计数器。把
它写成 `api-auth:v1` 契约的目的，是让这个取舍可被审阅，而不是让它看起来像一个完整的身份系统。

### 20.2 `api-auth:v1`：两个开关、一个判定顺序

`app/api/security.py` 只暴露两个协作对象。`ApiSecurityGuard` 持有鉴权模式与令牌摘要，
`SlidingWindowRateLimiter` 持有按身份的时间戳窗口。守卫的 `authorize()` 返回
`ApiSecurityRejection | None`，而不是抛 `HTTPException`：强制点是 ASGI 中间件，它位于路由之外，
FastAPI 的异常处理器捕获不到那里抛出的异常，返回值语义让中间件与单元测试共享同一份判定。

判定顺序是被测试固定的实现细节：**先限流，后鉴权**。若顺序相反，错误令牌会在 401 之前离开限流路径，
猜令牌就完全不受配额约束；反过来，把限流放在前面意味着即使请求最终是 401，它也已经消耗了这个来源
的配额。`tests/unit/test_api_security.py` 用 `max_requests=2` 断言第三次错误令牌返回 429 而不是
第三个 401，这样顺序被改动时测试会失败，而不是只有代码评审能发现。

令牌比较用 `hashlib.sha256` 摘要加 `hmac.compare_digest`：摘要让内存转储里不出现令牌原文，定长
比较让响应时间不随匹配前缀长度变化。解析保持严格——必须是 `Bearer <token>` 两段结构，scheme 按
RFC 7235 大小写不敏感，令牌部分只做 strip，因此"末尾多一个字符也算通过"这类隐式宽松不存在。

401 响应对"缺令牌"、"错方案"和"错令牌"完全一致：同一状态码、同一 `error_code`、同一 message、同一
`WWW-Authenticate`。任何差异都会把"这个实例是否配置了令牌"变成可探测信息，甚至帮助攻击者确认令牌
前缀。测试用集合断言 `len({rejection.message}) == 1` 锁定这一点。

### 20.3 强制点：前缀 fail-closed 的中间件

强制点是 `app/api/main.py` 的 `enforce_api_security` HTTP 中间件，判定依据是路径前缀
`("/api/v1", "/metrics")`。选择中间件而不是给每个路由加 `Depends`，是因为前者对**将来新增的路由
默认生效**：漏写一个依赖不会让新端点静默裸奔。代价是保护范围以前缀而不是路由为单位表达，所以前缀
集合本身必须被测试锁定，`tests/unit/test_api_security.py` 因此逐项断言它。

`/health` 和 `/demo` 刻意留在保护之外。`/health` 是容器存活探针，给它加鉴权意味着 healthcheck 需要
携带凭据，一旦令牌轮换容器就会被自己判定为不健康；它的响应也已经只含公开状态。`/demo` 只是无数据的
静态 HTML/CSS/JS，页面里的数据全部来自受保护的 `/api/v1`。

`/metrics` 在保护内，因为它不是探针而是运维接口：即使只有聚合数字，run 数量与各层错误数仍然会泄露
使用规模。Prometheus 抓取端支持 `bearer_token`，所以这个选择不会让指标变得无法采集。

守卫缺失时中间件返回 503 而不是放行。lifespan 未完成的实例不应该把"没有守卫"理解成"不需要守卫"，
这是与 `/api/v1` 在 runtime 未装配时返回 503（而不是伪造 200）一致的降级方向。

### 20.4 限流：滑动窗口、按 IP 计数、内存有界

限流器用滑动窗口而不是固定窗口计数器。固定窗口在边界处允许两倍突发——窗口末尾和下一窗口开头各打满
一次——而这里的配额本来就是为了保护付费调用与连接池。单个身份的时间戳数量天然被 `max_requests`
限制，因此无需额外裁剪。

配额按来源 IP 计数，**即使请求已经通过鉴权**。原因是本项目只有一个共享令牌：用令牌当键会让一个客户端
的突发耗尽所有合法客户端的配额。ASGI scope 在部分传输下没有 client 地址，这类请求归入固定的
`ip:unknown` 桶——牺牲精度，但"没有 IP"不能成为绕过配额的方法。

限流表的键来自不可信输入，它本身就是一个内存放大面。`MAX_TRACKED_IDENTITIES = 4096` 用 LRU 淘汰
最久未活动的身份，代价是极端情况下冷身份的配额被提前重置，这比 API 进程被限流表 OOM 更可接受。
淘汰只在成功记录请求之后执行，这样被限流来源的重试不会把正常来源挤出表外。

超限返回 429 并附带整数 `Retry-After`：值向上取整且至少为 1 秒，因为该头只接受整数秒，返回 0 会让
客户端立刻重试从而放大压力。超限时**不追加**时间戳，否则持续重试会不断把窗口向后推，把限流变成
永久封禁。

默认配额是 120 次 / 60 秒。这个数字不是随手取的：Demo 前端每轮轮询发两个请求、最短间隔 600ms 并
逐步退避到 4s，正常演示的峰值稳定低于该配额，而脚本化的暴力调用会立刻撞上它。

### 20.5 半配置拒绝启动与容器端口边界

配置面只有四个键：`DATAOPS_API_AUTH_MODE`（`disabled` | `bearer`）、`DATAOPS_API_AUTH_TOKEN`、
`DATAOPS_API_RATE_LIMIT_REQUESTS`、`DATAOPS_API_RATE_LIMIT_WINDOW_SECONDS`。校验分两层，与
`_validate_retrieval_providers` 同一个思路：任何"半配置"实例都必须拒绝启动，而不是降级运行。

`Settings._validate_api_security()` 拦两个方向。`bearer` 缺令牌会让每个请求都 401，等于服务不可用；
`disabled` 却配了令牌更危险——部署者会据此以为接口已受保护并把端口暴露出去。令牌强度和字符集由
`ApiSecurityGuard.__init__` 在 lifespan 阶段校验：至少 `MINIMUM_API_TOKEN_CHARS = 32` 个可见 ASCII
字符且不含空格。32 字符是 256 位随机令牌 base64 编码后的量级，低于此长度的"演示口令"在公网上等于
没有鉴权。最小长度只存在于守卫这一处，Settings 不复制它，避免两份常量漂移。

带空格或 CJK 的令牌同样在构造期失败：它们不能安全进入 HTTP 头，放宽只会造出"看起来配好了、实际永不
匹配"的实例。守卫在任何 Provider、MCP 子进程和数据库连接之前构造，所以这些错误等价于"拒绝开放端口"。

`compose.yaml` 的 API 端口绑定改成 `127.0.0.1:${DATAOPS_API_PORT:-18000}:8000`。默认部署的鉴权是
关闭的，绑到 `0.0.0.0` 等于把可触发付费模型调用的接口暴露到同网段。同一切片给 api 服务补上
`env_file: .env`：在此之前 `.env` 只参与 Compose 变量插值，容器内仍然跑仓库默认值，也就是说"在 .env
里配好令牌"根本不会生效。`.env.example` 明确要求令牌行保持注释而不是留空——空串会被解析成"已配置的
空令牌"，正好触发 disabled + token 的启动拒绝。

`/health` 增加 `security` 段，公开 `mode`、`contract_id`、受保护前缀列表和配额（次数与窗口成对给出，
只给"120"无法判断是每分钟还是每秒）。不公开令牌，也不公开令牌摘要：摘要虽不可逆，却会把"令牌是否
变更过"变成可观测信号，对排障没有价值，反而给离线字典攻击提供校验目标。

### 20.6 验证方式

```powershell
.venv\Scripts\python -m ruff check .
.venv\Scripts\python -m pytest -q tests/unit/test_api_security.py
.venv\Scripts\python -m pytest -q tests/integration/test_api_authentication.py tests/integration/test_health.py
docker compose config --quiet
```

`--quiet` 是刻意的：不带它的 `docker compose config` 会把插值后的完整配置打到 stdout，而插值输入是 `.env`，于是 `DATAOPS_CHAT_API_KEY` / `DATAOPS_DB_AUTH` / `DATAOPS_MCP_AUTH` 会以明文进入终端、shell 历史与 CI 日志。`--quiet` 保留全部校验能力（YAML 不合法或 `${VAR:?...}` 缺值仍非零退出），只是成功时不输出。要看拓扑用 `docker compose config --services`。

单元测试用可控时钟覆盖判定逻辑：受保护前缀集合、disabled 模式仍限流、精确令牌放行与五种变体返回逐字
相同的 401、限流先于鉴权、配额按身份独立且窗口滚出后恢复、匿名桶、LRU 淘汰后表大小收敛，以及五种
半配置/弱令牌组合在构造期抛错。集成测试则证明这些判定真的挂在请求路径上：bearer 模式下 `/api/v1` 与
`/metrics` 返回 401 且响应体不含令牌，`/health` 与 `/demo` 仍然公开，带正确令牌的请求得到 503（无数据库
时 runtime 未装配）——恰好说明它已穿过鉴权进入路由；配额耗尽返回 429 且 `Retry-After` 可解析为正整数。

契约版本 `api-auth:v1` 同时写在 `app/core/settings.py` 的 `api_auth_contract_id`、
`app/api/security.py` 的 `API_AUTH_CONTRACT_ID`、`/health` 的 `contracts.api_auth` 与 lifespan 断言里，
任一处不一致都会拒绝启动。

## 21. 运行状态 SSE 增量推流

### 21.1 为什么加推流，以及为什么轮询必须保留

一次真实诊断要串起 Planner 决策、九个 MCP 子进程往返、两条检索通道和一次独立 Auditor 审计，P95 目标是
≤30 秒。纯轮询的问题不是"技术落后"，而是它把延迟下限锁在轮询间隔上：间隔调小就放大数据库读放大，
调大则用户在一段明显的空白里看不到任何进展。SSE 解决的正是这段"已经发生但还没被读到"的延迟。

但推流**不是第二条执行路径**。run 依旧由 PostgreSQL Worker 用 `FOR UPDATE SKIP LOCKED` + 租约执行，
`GET /api/v1/runs/{run_id}/stream` 只做只读增量投递。这条边界被写进类型：`iter_run_stream` 只接受
`RunStreamSource` 协议——两个只读方法 `get_run` 与 `get_events_after`，没有 workflow、没有写事务、
没有 Worker 接口。因此"一条挂着的浏览器连接推进了 run"在结构上不可能发生，而不是靠评审纪律避免。

也因此轮询是**永久保留的等价通道**而不是过渡方案：断流不改变任何结论，前端任何时候都可以退回
`GET /api/v1/runs/{run_id}` + `/events`。这一点在 `api-auth:v1` 下从"可选"变成"必需"，见 21.5。

### 21.2 三种帧与终止原因分类

`app/api/streaming.py` 定义 `run-stream:v1`，帧只有三种，由 SSE 的 `event:` 字段命名：

| 帧 | 何时发送 | 载荷 |
|---|---|---|
| `run_snapshot` | 状态**发生变化**时（含首跳） | `status` |
| `run_event` | 每条新的公开事件 | `event`（`RunPublicEvent`） |
| `stream_end` | 收尾，恰好一次 | `end_reason` |

`run_snapshot` 只在状态变化时发，而不是每 0.5 秒重复一遍 `running`——否则一次 30 秒的 run 会产生
六十帧无信息内容的噪声。`RunStreamFrame` 是 `extra="forbid"` + `frozen=True` 的模型，`model_validator`
强制两条交叉约束：`run_event` 必须带 `event` 且 `event.run_id == run_id`、`event.sequence == cursor`；
非 `run_event` 帧带 `event` 直接失败。把游标和已投递事件绑死在类型层面，是因为这两者一旦分叉，
重连续传就会静默丢事件或重放事件，而这类 bug 在时间线上只表现为"偶尔少一行"，极难复现。

`end_reason` 刻意分成两类，这是整个契约里最值得讲的产品判断：

- `completed` / `failed` / `cancelled` —— **run 级**终态，客户端应停止重连；
- `stream_timeout` / `run_disappeared` —— **连接级**终止，客户端应带最后游标重连或退回轮询。

如果把两者合并成一个 `closed`，前端就无法区分"诊断结束了"和"连接断了"，只能二选一地犯错：要么把
超时渲染成失败（用户以为诊断挂了），要么在真正失败后无限重连。

### 21.3 每跳的读取顺序：一条被测试钉住的不变量

`iter_run_stream` 每一跳严格按 **先读 run 快照 → 再读增量事件 → 最后用那个较早的快照判定终态** 执行：

```python
run = await source.get_run(run_id)            # 1. 先拍快照
...
events = await source.get_events_after(run_id, after_sequence=cursor)   # 2. 再取增量
for event in events or ():
    cursor = event.sequence
    yield ...
if run.status in TERMINAL_RUN_STATUSES:       # 3. 用第 1 步的快照判定
    yield _end_frame(...)
    return
```

原因是 `complete_run` 把终态与最后一批事件放在**同一事务**提交。若先读事件再读快照，就存在这样的
交错：读事件（拿到旧的一批）→ Worker 提交（终态 + 最后几条事件）→ 读快照（已是终态）→ 立刻发
`stream_end`。结果是时间线缺了最后几条事件却宣称流已正常结束。反过来按现在的顺序，最坏情况只是
终态那一跳读到的还是 `running`，于是多轮询一跳再收尾——多一次 0.5 秒轮询换取"`stream_end` 之前
时间线一定完整"。`tests/unit/test_run_stream.py::test_stream_reads_run_before_events_so_terminal_tick_keeps_final_events`
用脚本化替身把这个顺序固定住：终态那一跳同时给出三条事件，断言它们全部先被投递、`cursor == 3`。

`get_events_after` 用 `None` 表示"run 不存在"、用空元组表示"这一跳没有新事件"，两者驱动完全不同的
分支（前者 `run_disappeared` 收尾，后者继续轮询）。过滤下推到 SQL（`sequence > after_sequence`），
因此一条长连接的每次轮询只取真正的新增行，而不是每跳把整条时间线搬进内存再切片——领域模型
`RunEventList` 要求序号恰好覆盖 1..N，本来也无法承载增量结果，所以仓储层另开
`list_events_after` 返回裸元组。

### 21.4 `Last-Event-ID` 续传：不发明协议

`cursor` 被写进标准 SSE `id:` 字段，浏览器因此在自动重连时带上 `Last-Event-ID` 头，**不需要任何自定义
续传协议**。`resolve_stream_cursor` 规定 `Last-Event-ID` 优先于 `?after_sequence=`；非整数或负值回退到
查询参数值，而不是从 0 重放——把损坏的头部当成"从头再来"会让用户在时间线上看到重复事件。

单连接寿命由三个预算约束，`Settings._validate_run_stream()` 强制它们依次递增：

```
run_stream_poll_seconds (0.5) < run_stream_keepalive_seconds (15) < run_stream_max_seconds (300)
run_stream_max_seconds > react_total_timeout_seconds (240)
```

最后一条最容易被忽略：如果最长寿命不大于 ReAct 总超时，一个**只是跑满预算的正常 run** 会在结束前
被推流截断，看起来像系统不稳定。心跳交给 `EventSourceResponse(..., ping=int(keepalive_seconds))`，
不自己发注释帧——反向代理需要的只是"连接上有字节流动"，重复实现只会多一处可写错的地方。

`RunStreamConfig` 在路由里构造而不是加一个 `Settings.run_stream_config()` 工厂方法：后者会让
`app.core` 反向导入 `app.api`，把分层依赖倒过来。

### 21.5 鉴权模式下的诚实限制

浏览器 `EventSource` 无法设置请求头，因此 `api-auth:v1` 切到 `bearer` 后，`/api/v1` 前缀中间件一定拒绝
推流请求。这是被接受的限制而不是缺陷——绕过它需要把令牌放进查询字符串（会进日志和 Referer）或
额外引入一层 cookie 会话，两者都比"退回轮询"更糟。系统的做法是把它变成可观测事实：`/health` 的
`stream.available_under_auth` 随鉴权模式变化而不是硬编码 `true`，前端 `app.js` 在推流被拒时把
`poll-message` 写成"…，已退回轮询"并继续正常工作。

帧内禁止出现 Thought、原始思维链、Prompt、embedding、凭据或 Provider 原始响应。这里不需要新的过滤
规则：帧只承载 `RunPublicEvent`，那已经是过滤后的公开投影，推流的义务是**不旁路它另开字段**。
前端 `appendTimelineEvent` 复用 `createTimelineItem` 并按 `sequence` 去重（`state.renderedSequences`），
因此重连重放同一事件不会在页面上出现两行，也不会用不可信数据赋值 `innerHTML`。

### 21.6 验证方式

```powershell
.venv\Scripts\python -m ruff check .
.venv\Scripts\python -m pytest -q tests/unit/test_run_stream.py
.venv\Scripts\python -m pytest -q tests/integration/test_diagnosis_api.py tests/integration/test_health.py
```

单元测试覆盖判定逻辑：状态只在变化时推一帧、事件按序投递且游标随之推进、终态跳先投完事件再收尾、
带游标续传不重放已知事件、run 消失收成 `run_disappeared` 而不是 `failed`、寿命耗尽在**仍然活着**的 run
上收成 `stream_timeout`（用可无限轮询的替身并断言 `polls > 1`，证明是预算而不是数据枯竭停下了循环）、
三种帧的可选载荷交叉校验，以及三个预算的有序性。集成测试则用手写的 `event:`/`id:`/`data:` 行解析器
证明这些判定真的挂在 HTTP 上：帧名依次为 `run_snapshot` / `run_event` / `stream_end`、`id` 字段确实
承载游标、`Last-Event-ID` 让重连不再收到 `run_event`、未知 run 返回真正的 404（而不是流内一帧）、
runtime 未装配返回 503，且响应体不含 `Thought` 与 `reasoning_process`。

契约版本 `run-stream:v1` 同时写在 `app/core/settings.py` 的 `run_stream_contract_id`、
`app/api/streaming.py` 的 `RUN_STREAM_CONTRACT_ID`、`/health` 的 `contracts.run_stream` 与 lifespan
断言里，任一处不一致都会拒绝启动。

## 22. 双 Agent 契约：否决权、返工预算与降级阶梯

第 12 节讲的是 Auditor 这条链路怎么实现，本节讲的是**两个 Agent 之间的契约本身**：谁能否决谁、
返工预算属于谁、失败时往哪一级降级，以及这条阶梯在 API、trace 和前端各暴露到什么程度。这是整个
系统里最容易被做成"装饰性审计"的地方——加一个 Auditor 节点很容易，让它真的拥有否决权很难。

### 22.1 为什么是两个 Agent，而不是让 Planner 自评

自评的失效模式是结构性的，不是提示词能修的：同一次调用里生成结论和评价结论，模型的目标函数是
自洽而不是正确，它会为已经写下的根因寻找支持，而不是寻找反例。所以本系统把审计做成**独立
Agent**，且独立性落在四个可检查的地方：

| 隔离维度 | Planner | Auditor |
|---|---|---|
| Prompt 契约 | `planner-react:v8` | `auditor-report:v2` |
| Provider 契约 | `openai-compatible-planner:v1` | `openai-compatible-auditor:v1` |
| 输出 Schema | `PlannerDecision`（actions 数组） | `AuditResult`（accept/revise + 有限问题码） |
| Schema 修复预算 | `planner_schema_repair_count` | `auditor_schema_repair_count` |

两者共用同一份 Chat 端点配置（同一个模型也可以），但**不共享上下文**：Auditor 看到的是终态报告、
实时 Evidence/ToolEvent、GraphRAG Bundle、confirmed 案例和确定性问题，看不到 Planner 的决策过程。
反向也成立：Planner 不知道审计规则的判定结果，因此无法反向拟合门禁。

Auditor 的输出空间被刻意限制为 `accept | revise` 加七个问题码，不允许自由文本状态。这条限制的作用
是让**控制流不依赖自然语言解析**：`_route_after_audit` 只读结构化 `AuditStatus` 与 `retry_count`，
从不读 `revision_instructions`。模型可以写修订建议，但它无法用措辞改变图的走向。

### 22.2 否决权矩阵：确定性规则可以推翻模型的 accept

审计有两层，且优先级是明确的**非对称**关系：

| 层 | 实现 | 擅长判定 | 能否否决对方 |
|---|---|---|---|
| 确定性规则 | `app/reporting/policy.py` | ID 是否存在、字段是否齐全、案例是否 confirmed、high 风险是否带引用与前置条件 | **能**：任何规则问题非空即强制 `revise` |
| 独立 Auditor | `auditor-report:v2` | 引用文本是否真的支持结论、实时结果与历史/知识是否语义冲突 | 不能推翻规则，只能追加语义问题 |

合并发生在 `_merge_audit_result`：规则问题非空时，无论模型返回什么，结果一律是 `revise`，规则问题
排在前面，并追加一条通用指令"先修复全部确定性引用、冲突和风险问题，再重新提交独立审计"。去重键
是 `(code, claim_path, evidence_refs)`——**故意不含 `message`**，否则同一个问题因为 Auditor 换了措辞
就会重复计数，让"问题数"这个可观测数字失去意义。

反过来，规则全过而 Auditor 返回 `revise` 时，报告同样不能放行。所以放行条件是**两层都同意**，
而降级条件是**任一层不同意且预算耗尽**。这条不对称是有意的：漏放一份正确报告的代价是用户多看
一次"需要补证"，误放一份错误报告的代价是有人按它去动生产系统。

七个问题码（`invalid_evidence_ref` / `unsupported_claim` / `evidence_conflict` /
`missing_risk_control` / `unconfirmed_case` / `report_incomplete` / `auditor_unavailable`）是封闭集合，
模型不能自造新码。这不是为了整洁，而是因为每个码都对应下游一个确定性动作；允许模型扩张码集，
等于允许它新增工作流分支。

### 22.3 降级阶梯：四级，每一级都有明确的触发条件与终态

```
draft_report ──▶ audit_report ──accept + 无规则问题──▶ accepted（唯一放行出口）
                     │
                     ├─ revise 且 retry_count < max_revisions ──▶ revise_report ──▶ 回到 audit_report
                     │
                     ├─ revise 且 预算耗尽 ─────────────────────▶ degrade_report ──▶ degraded
                     │
                     └─ AuditorAgentError（Provider/refusal/Schema）─▶ 立即 degraded（不消耗返工预算）
```

| 级别 | 触发 | 动作 | 终态 |
|---|---|---|---|
| 0 放行 | 两层都 accept | 不改报告 | `accepted` |
| 1 返工 | `revise` 且还有预算 | `SafeReportReviser.revise`：只删不加 | 回到审计，`retry_count += 1` |
| 2 降级 | `revise` 且预算为 0 | `SafeReportReviser.degrade`：清空未放行结论 | `degraded` |
| 3 不可用降级 | Auditor 抛错 | 合成 `auditor_unavailable` 问题后直接 degrade | `degraded` |

三条被代码断言钉住的不变量：

1. **`accept` 必须已经把工作流标成 completed。** `_route_after_audit` 对"accept 却还在路由"直接
   `raise RuntimeError`，而不是宽容地当成结束——静默容错会让"放行"多出一条没被测试覆盖的路径。
2. **返工只能在预算内进入。** `_revise_report` 入口再次校验 `retry_count < max_revisions`，即使路由
   写错也不会出现第二次返工。预算属于**工作流**而不是模型：`ReportWorkflowConfig.max_revisions`
   被 Pydantic 限制在 0–1 且模型冻结，运行中 Agent 无法扩大它。
3. **返工后必须重新走完整审计。** `_revise_report` 把 `audit_result` 置为 `None`，因此新草稿不可能
   沿用上一轮的 accept/revise；`_audit_report` 每轮都**重新**跑一遍确定性规则，而不是复用首轮
   issues——否则修订把引用改对了，旧问题仍会把它误判为不合格。

第 1 级的"只删不加"是这条阶梯的安全基础：`SafeReportReviser` 只过滤悬空引用、删除不被支持或与
证据冲突的根因、移除未确认案例、把 high 风险建议收窄为只读补证。它**永远不会**新增事实或提高
置信度。因此返工在数学上不可能把一份错报告改成一份更自信的错报告，最坏结果只是收窄到降级。

删除的**粒度**由 `AuditIssue.claim_path` 决定：`$.root_causes[1]` 只删第二条根因，`[i:j]` 是半开
区间，`summary` / `risks` / `evidence_refs` 这类派生字段被指向时不删任何结论而是重算，路径无法解析
或指向未知字段时退回整类删除（根因 + 链路 + 相似案例）。首次真实模型评测暴露了整类清空的自我
拆台效应：案例 1 的 ReAct 状态里已经有两条 `supported` 假设（含 Golden 根因），首轮把根因和链路
全部清空后，第二轮 Auditor 对修订稿自己写下的"证据不足"表述提出 `evidence_conflict`，一次返工
预算耗尽后只能 `safe_degraded`。定位删除只改粒度不改性质——修订稿仍要重新通过确定性规则和
独立 Auditor，所以更精确的删除不可能放行不合格报告，`audited-report-workflow:v2` 的拓扑与状态
Schema 也无需升版本。

第 3 级刻意**不消耗返工预算、也不重跑报告**。Auditor 不可用不是报告的问题，重跑同一份草稿只会
再撞一次同样的故障，并把一次外部故障放大成两次付费调用。系统的选择是立即合成一条
`auditor_unavailable` 问题并降级——"审计不可用"绝不等于"审计通过"。

正因为第 3 级不消耗预算就直接降级，Auditor 的超时必须单独配置。首次真实模型评测把这条依赖关系
暴露得很清楚：两个角色共用 `DATAOPS_CHAT_TIMEOUT_SECONDS=30` 时，Planner 单次实测 8–15 秒安全
通过，Auditor 单次实测 22–30 秒（它要读完整草稿并逐条核对引用，输出接近 1100 token），四次审计
调用有三次超时，三次超时全部按第 3 级降级，于是三个案例的 `accepted_report_rate` 实测为 0——审计
阶梯的第 0 和第 1 级根本没有机会执行。修复是新增 `DATAOPS_AUDITOR_TIMEOUT_SECONDS`（默认 90 秒）
而不是放宽共用值：Planner 跑在 `react_total_timeout_seconds` 预算内，一次挂死就吃掉整轮取证，必须
保持紧超时；Auditor 不在该预算内，放宽它不会拖长 ReAct 循环。`/health` 的 `planner.timeout_seconds`
与 `auditor.timeout_seconds` 分别暴露这两个值，部署者不读代码也能确认它们不是同一个旋钮。

`degraded` 的下游后果是硬性的：`stage_case_memory` 只接收 accepted 且含根因的报告，因此**降级
报告永远不会进入长期记忆**。这条链路保证了错误结论不会通过"学习"变成下一次诊断的历史证据。

### 22.4 当前明确保留的边界

返工只覆盖**报告级收窄**。如果 Auditor 判断必须补充新的实时 Observation 才能放行，本版本返回降级
报告加只读补证步骤，而**没有**把边重新接回 Planner ReAct。这是有意保留而不是遗漏：把审计结论接回
ReAct 会引入"审计驱动取证"的新回路，它需要自己的步数预算、循环检测和成本上限，否则两个 Agent
可以互相触发调查直到打满预算。用同一批旧证据重复推理没有信息增益，所以当前版本宁可诚实降级。

### 22.5 这条阶梯在四个出口暴露到什么程度

同一条审计轨迹按"读者需要多少"分四档暴露，粒度不同是刻意设计：

| 出口 | 暴露内容 | 不暴露 |
|---|---|---|
| `GET /api/v1/runs/{run_id}` | 完整 `report.outcome`、`state.audit_result`（含 `issues[].message` 与 `revision_instructions`）、`report.events` | Prompt、Thought、Provider 原始响应 |
| `GET /api/v1/runs/{run_id}/events` | 四类报告事件 + `audit_status` + `issue_codes` + `revision_number` | 问题的自然语言 message |
| `GET /api/v1/runs/{run_id}/trace` | `report.draft` / `report.audit` / `report.revise` / `report.degrade` 节点 span，以及 `auditor.review` 模型 span 上的 `audit_status`、`deterministic_issue_count`、`model_issue_count` | 任何非 ASCII 标识符的属性值 |
| `/demo` 审计裁决卡 | outcome、Auditor 结论、`已用返工 n / 1`、问题码与被否决的 `claim_path`、`evidence_refs` | `issues[].message`、`revision_instructions` |

`run_events` 与前端刻意只保留问题码而不保留 message，理由同一条：**未经放行的表述不应以"审计
意见"的形式重新出现在页面上**。message 是模型对"为什么不能放行"的解释，它本身没有经过审计；把它
渲染成审计结论，等于让被否决的自然语言从侧门回到读者眼前。API 仍完整返回它，因为调试和评测需要
它——区别在于那是显式请求单个 run 的开发者行为，不是演示页面的默认视觉输出。

`auditor.review` 的 span 只包住**模型往返**，确定性规则校验在它之外完成。这不是实现细节：把规则
耗时算进 Auditor，会让"Auditor 有多慢"这个数字系统性偏大，从而误导后续的性能取舍。

前端把裁决卡放在报告正文**之前**，并让 `degraded` 使用与失败态相同的红色语义。理由是阅读顺序即
风险顺序：读者必须先知道这份结论有没有被放行，再去读它写了什么；如果降级报告和放行报告长得一样，
"安全降级"这个机制在演示中就等于不存在。

### 22.6 验证方式

```powershell
.venv\Scripts\python -m ruff check .
.venv\Scripts\python -m pytest -q tests/unit/test_report_workflow.py tests/unit/test_reporting.py
.venv\Scripts\python -m pytest -q tests/integration/test_openai_compatible_auditor.py tests/integration/test_demo_frontend.py
.venv\Scripts\python -m app.evaluation --skip-postgres
```

`auditor-impact-eval:v1`（第 16.3 节）是这条契约的量化验证：三条语义缺陷案例分别在"只有规则"和
"规则 + Auditor"两种配置下运行，归因增量拦截、危险残留与安全处置。当前成绩来自确定性脚本替身的
小样本，**不能外推为真实 LLM 的审计质量**；这条限制必须保留在报告和任何对外材料里。

## 23. 模型调用的有界瞬时重试

### 23.1 为什么这一条是实测逼出来的，而不是照抄最佳实践

第二次真实模型 Golden 冒烟给出了一段很干净的证据：同一个端点在约 92 秒内成功完成 6 次调用
（单次 8.5–22.9 秒），随后连续 4 次返回 HTTP 错误，每次只用 0.26–0.63 秒就被驳回。响应时间差两个
数量级，说明后 4 次根本没打到模型，是网关按配额窗口直接拒绝的。后果不是"慢一点"：两个案例以
`planner_provider_error` 终结，其中一个连第一个工具都没执行，`necessary_action_coverage` 直接归零。

在此之前，`PlannerProviderError.retryable` 已经存在但没有任何消费者——它只是一个诊断属性。所以这
不是"发现了 bug"，而是一个被文档记录过的**有意推迟**：`model-transient-retry:v1` 把那个标记接上了
真正的控制流。

### 23.2 重试为什么放在包装层，而不是 Provider 内部

`app/agents/retrying.py` 提供 `RetryingPlannerChatProvider` / `RetryingAuditorChatProvider` 两个包装器，
它们实现同样的 `complete` 协议，Agent 适配层察觉不到差别。真正的原因是保住两条既有边界：

1. **具体 Provider 继续保持 `max_retries=0`，一次 `complete` 只发一次网络请求。** 因此每次尝试仍各自
   产生一条 `model-call-metric:v1` 记录和一个 `model_call` span——"第一次 429、第二次成功"在遥测里是
   两条可归因的记录，而不是被平均掉的一条。如果把重试塞进 `OpenAICompatiblePlannerProvider.complete`
   里，SDK 层的隐藏重试问题会以我们自己的形式复现：延迟统计会把退避等待算进模型耗时，错误率会
   凭空下降，而"配额窗口被打满"这个最需要被看见的事实会消失。
2. **连接池所有权不变。** 包装器不持有 HTTP 资源，也**不提供 `aclose`**；`PlannerRuntime` /
   `AuditorRuntime` 仍持有具体 Provider，lifespan 退出时关闭的还是那一层。

与 MCP 侧 `McpToolExecutor` 的策略刻意对称：同一套"只重复供应商已判定为瞬时的失败"的语义，
在工具边界和模型边界各实现一次，而不是抽象成一个跨层的通用重试装饰器。

### 23.3 什么会被重试，什么绝不重试

| 失败 | `retryable` | 行为 |
|---|---|---|
| HTTP 429、5xx | true | 退避后重发同一批消息 |
| 超时、连接失败 | true | 同上 |
| HTTP 401/403 | **false** | 一次即上抛，且完全不进入退避 |
| Schema 不合法（`PlannerOutputValidationError`） | 不适用 | 兄弟异常，重试层看不见；归 `repair_count` |
| 结构化 refusal | 不适用 | 同上，重发规避安全判断是不允许的 |

认证失败被显式排除：重复投递坏凭据既救不回调用，也会加速触发网关封禁。**瞬时重试与 Schema 修复
是两套预算，故意不合并**——传输失败不该消耗格式修复预算，格式错误也不该触发退避等待。

重试逐次**原样重发**同一批消息。重试成立的前提正是"这次调用没有产生任何副作用"；改写内容会让第二
次尝试变成语义不同的请求，也就把重试悄悄变成了第二轮决策。

### 23.4 预算：为什么 `react_total_timeout_seconds` 必须同步放宽到 240 秒

`TransientRetryPolicy` 默认 `max_attempts=2`、退避 1s 起、倍数 2、上限 8s。上限被 Schema 钉在三次
尝试：实测的配额窗口打满形态重试更多次救不回来，只会把单次决策的最坏耗时推出墙钟预算。也**不加
随机抖动**——本系统的并发度是单个诊断 run，抖动只会让实测耗时不可复现。

`worst_case_added_seconds(single_call_timeout)` 把"重试要多花多少时间"变成一个可校验的数字：
重试次数 × 单次超时 + 有界退避和。默认配置下是 `1 × 30 + 1 = 31` 秒。`Settings._validate_transient_retry()`
据此在启动阶段强制 `react_total_timeout_seconds ≥ chat_timeout_seconds + worst_case_added_seconds`，
默认值因此从 150 秒放宽到 **240 秒**。

这条校验存在的理由，就是防止一个只做一半的加固：如果墙钟预算不变，第二次尝试会在退避途中被
`asyncio.timeout` 掐断，run 以 `total_timeout` 收口——等于**加了重试又不让它生效**，而且把
"预算够但时间不够"伪装成了正常终止。反过来，预算耗尽时包装层**原样上抛**最后一次失败，ReAct 循环
照旧以 `planner_provider_error` 收口；审计侧照旧走第 3 级不可用降级，不消耗返工预算。重试只争取让
审计真的跑起来，**绝不因为网络失败而放行报告**。

三个旋钮：`DATAOPS_CHAT_TRANSIENT_RETRY_ATTEMPTS`（默认 2）、
`DATAOPS_CHAT_TRANSIENT_RETRY_BACKOFF_SECONDS`（默认 1）、
`DATAOPS_CHAT_TRANSIENT_RETRY_MAX_BACKOFF_SECONDS`（默认 8）。两个角色共用同一份策略是有意的：
它们打同一个端点、共享同一份配额，分别配置只会让"到底哪一层在退避"变得难以推断。`/health` 的
`limits.chat_transient_retry_attempts` 与 `tool_retry_count` 并列公开，部署者不读代码也能确认模型侧和
工具侧是两套独立预算。

### 23.5 验证方式

```powershell
.venv\Scripts\python -m ruff check .
.venv\Scripts\python -m pytest -q tests/unit/test_model_transient_retry.py tests/unit/test_planner_factory.py
.venv\Scripts\python -m pytest -q tests/integration/test_health.py
```

单元测试注入记录型 `sleep` 与脚本化 Provider 替身，因此可以在毫秒内断言退避序列恰好是 `[1.0]` 或
`[1.0, 2.0]`、认证失败一次即抛且 `delays == []`、预算耗尽后上抛的仍是**最后那个**
`PlannerProviderError` 实例。工厂测试断言 Agent 拿到的是包装器、而 `runtime.provider` 仍是具体
Provider——这条接线是重试能生效的唯一途径。

真实端点侧只有半条证据，必须如实区分：加上重试后的干净运行（Run F）根本没有出现瞬时失败，所以它
只证明链路仍然正常；另一次被丢弃的备选模型探测运行则相反——12 条模型调用记录恰好是 3 案例 ×
（Planner 2 次 + Auditor 2 次），说明退避在真实端点上确实按 `max_attempts=2` 重发并且每次尝试各自
落成一条独立遥测记录，但两次尝试都撞上同一个网关容量错误，因此**"重试成功救回调用"仍然没有真实
端点证据**。两次运行的口径记录在 `docs/live-golden-eval-results.md`。






