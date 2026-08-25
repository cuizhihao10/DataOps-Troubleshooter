# DataOps Troubleshooter

面向公开作品集的、证据驱动的大数据链路智能排障 Agent。当前已经完成领域契约、九工具真实 MCP、GraphRAG 与文档 RAG 双知识通道、五项固定 capability、LangGraph 有界循环、Planner/Auditor OpenAI-compatible Structured Outputs，以及受控长期案例记忆。

本项目同时是学习与求职展示项目。代码中的模块级说明、每个 callable 的详细 docstring 和复杂函数关键步骤注释负责解释局部设计，完整技术原理、数据流、设计取舍和验证方法统一维护在 [`docs/implementation-guide.md`](docs/implementation-guide.md)。

## 当前切片

- 固定产品设计中的 9 个只读 MCP 工具名称。
- 使用 Pydantic 定义 Planner 决策、MCP 请求/响应、Evidence、AgentState、报告和案例记忆契约。
- 提供 18 个脱敏且确定性的合成场景，以及对应 Golden Case 格式。
- 启动时校验全部 Fixture 和 Golden Case 引用。
- 提供 `GET /health`，返回契约版本、运行预算和已加载场景。
- 通过官方 MCP Python SDK 和 stdio 协议暴露产品规定的 9 个只读工具，并将返回标准化为 Evidence 与 ToolEvent。
- 瞬时错误最多自动重试一次，每次尝试均保留独立 ToolEvent；空结果和权限错误不会重试。
- PostgreSQL + pgvector 保存 `graph-seed:v12` 的 54 个显式知识节点、71 条关系边和带 Provider 溯源的向量，支持全文/向量混合召回、五项可解释评分与 1–2 跳路径扩展。方案类节点显式声明 `remediation_risk_level`，报告里的处置风险等级只能来自这条人工声明，不从动作文本猜、也没有默认值。
- Evidence Bundle 按 UTF-8 JSON 字节、节点数和路径数三重预算原子选择证据，并返回稳定 `kn_*` / `path_*` 引用与 omitted IDs。
- 版本控制的消融案例真实比较 vector-only 与 vector+graph；当前实测根因命中持平，必要因果链完整率由 0.0 提升至 1.0。
- `document-retrieval:v1` 以文档 RAG 作为第二条知识通道：`document-seed:v1` 的 5 份脱敏 Runbook/SOP/复盘/FAQ 按标题层级切片入库，切片而不是文档是检索与引用单元，引用 ID `dc_*` 同时充当 `document_chunks` 主键与报告脚注。
- 文档通道用 `ts_rank` 全文与 pgvector cosine 双路召回，按语义 0.60、全文 0.25、权威度 0.15 三因子评分；向量召回必须同时匹配 Provider ID 与维度，两个向量空间不会被放进同一次排序。
- 两阶段检索先按倍数多召回候选，再由 `BAAI/bge-reranker-v2-m3` 重排并按 `final_score = (1 - w) * hybrid + w * rerank` 融合；重排器不可用或分数条数不齐时整体降级为一阶段排序并把 `reranker_model` 留空，绝不把一阶段排序说成精排结果。
- 报告只把 Runbook/SOP 的步骤小节提升为处置建议，复盘“改进项”、FAQ 与“禁止操作”即使被召回也只能作为证据；文档切片与图证据共用同一字节预算但拥有独立条数上限，加载顺序为路径 → 种子节点 → 文档切片，被裁切片 ID 出现在 `omitted_chunk_ids`。
- 五项 capability 以 `runtime-capabilities:v1` 输出 Prompt 片段、工具优先级、输入要求和输出规则；历史匹配仅按需启用，实时 Observation 始终优先。
- `langgraph-react-loop:v3` 真实执行 capability 注入、Planner 决策、MCP Action、Observation 回写和回到 Planner，并把 raw confirmed 案例与确定性解释绑定后注入 Planner。
- 同一轮可提交 1–3 个互不依赖的只读 Action，批内经 `asyncio.gather` 真并发跨独立 stdio MCP 子进程执行；一批 N 个 Action 仍消耗 N 个步数，并行只压缩等待时间而不发放额外取证预算，任一门禁不通过整批拒绝而不截断。
- `planner-react:v8` 隔离 system/user 数据，注入同会话上一轮报告、历史案例共同点/差异点/参考动作/避坑提示，以及由渲染层算好的剩余步数、本轮批次上限、`trace_id`、带 `source` 标注的可引用 ID 白名单（与报告层同一份来源映射）和尚未执行的优先级工具；`hypothesis_updates` 是模型结论进入报告根因的唯一通道（`decision_summary` 不会被解析），升为 supported 只认实时 Observation 引用，`stop_reason` 收敛为七个可评测枚举值。Structured Outputs 仍只返回结构化 Action 数组，批次上限由 Pydantic 校验器执行（strict Schema 不接受 `maxItems`）。
- 假设更新经同一道引用白名单门禁后确定性投影进 `AgentState.hypotheses`：组件取本次已批准的 capability 组件，置信度按状态映射（candidate 0.4 / supported 0.7 / rejected 0），模型不能自报这两项，报告里也就不会出现无法复算的自评数字。
- 确定性 Builder 只把有有效支持引用且无反对证据的假设提升为根因；链路和建议分别引用 `path_id` 与知识节点证据。
- `auditor-report:v2` 使用独立 Structured Outputs Agent 审核实时事实与历史解释冲突；`audited-report-workflow:v2` 的确定性问题可否决错误 accept，最多返工一次。
- `case-memory:v2` 只接收 Auditor accepted 且含根因的报告，新候选默认为 pending；exact signature 优先、pgvector cosine 次之，同 run 重放不会重复增加 occurrence。
- `POST /api/v1/memories/{memory_id}/confirm` 支持 confirm、reject 和重新 confirm；`DELETE /api/v1/memories/{memory_id}` 在事务内删除案例、证据关联和动态图节点；`GET /api/v1/memories/search` 只返回 confirmed 案例，数据库未启用时明确返回 503。
- confirmed 案例在同一事务注册为 GraphRAG `case` 节点，复用记忆 embedding，并按独立阈值建立稳定双向 `SIMILAR_TO`；reject 删除节点并级联清边。
- 历史召回合并 pgvector 直接 top-k 与 `SIMILAR_TO` 图邻居，公开 vector/graph 通道、直接相似度、图传播分和稳定 edge 引用。
- `memory-recall-eval:v1` 使用 6 条合成查询真实比较 vector-only/vector+graph；当前受控样本 Macro Recall@K 与 Precision@K 实测从 0.9167 变为 1.0000，禁止案例命中为 0。
- `history-impact-eval:v1` 使用 3 条合成诊断真实比较 Memory off/on；确定性 LangGraph 小样本中必要 Action 覆盖实测从 0.6667 变为 1.0000，意外 Action 率从 0.3333 降为 0，根因命中、实时引用、历史投影和冲突保护均保持 1.0000。
- `auditor-impact-eval:v1` 使用 3 条语义缺陷案例比较规则对照与完整 Auditor；预期问题发现率实测从 0 变为 1.0000，危险内容残留率从 1.0000 降为 0，安全处置率从 0 变为 1.0000，其中两例修订后接受、一例持续冲突后降级。
- `golden-case:v8` 同时覆盖安全降级、反证、Schema、检查点、倾斜、限流、授权和水位线时区传播；当前 28 条案例使用 18 个脱敏 Fixture，类别配额完整达到 8/10/4/3/3，并为其中 14 条声明知识图 `root_cause` 节点锚点。
- `golden-diagnosis-eval:v23` 要求三层 900 条缺口闭合，并用 `WATERMARK_TIMEZONE_MISMATCH` 与一致性抽检识别“同步完成但静默漏数”；当前 28/28 确定性脚本满分不冒充真实 LLM 成绩。新增 `root_cause_anchor_hit_rate` 直接判定 Top-1 根因是否引用了正确的 `kn_root_cause_*` 节点，它与文本相等的 `root_cause_top1_hit_rate` 是两个分母不同的独立指标，必须并列阅读、不可相减。
- `portfolio-eval-run:v23` 通过 `python -m app.evaluation` 一次执行五层、20 个独立指标。
- `live-golden-eval:v1` 提供显式 opt-in 的真实模型三案例冒烟评测，经生产 PostgreSQL GraphRAG、双 Agent、LangGraph 与 stdio MCP 执行，并只记录版本、状态、耗时和 token，不记录 Prompt、原始响应或 Thought。已在固定 `gpt-5.6-sol` 端点执行多轮：`planner-react:v8` + 定位修订下必要 Action 覆盖与证据来源覆盖实测 1.0000（v7 为 0.7778）、必要因果链完整率实测 0.6667（v7 为 0.1667）、风险等级命中实测 0.6667（v7 为 0.3333），同时模型调用从 15 次降到 12 次。`root_cause_top1_hit_rate` 实测仍为 0，原因是评分器用精确字符串比较根因与知识节点名；`golden-diagnosis-eval:v23` 新增的 `root_cause_anchor_hit_rate` 在最近一轮实测 0.500（分母 `anchored_case_count=2`），它是与文本相等口径**并列**发布的独立指标，不是把 0 提升成 0.5——两者分母与判定口径都不同，文本相等口径一个字未改。三案例 smoke 不能外推到 28 条，P95 ≤ 30 s 仍是设计目标值而非实测值（实测每案例 58–125 s），完整口径与未达标项见 [`docs/live-golden-eval-results.md`](docs/live-golden-eval-results.md)。
- `audited-diagnosis-workflow:v2` 按 history trigger 召回 confirmed 案例，在 ReAct 前后两次确定性比较同批候选，再串联独立 Auditor 和审计后 memory staging。
- `diagnosis-resources:v4` 提供 session/message/run/event PostgreSQL 资源，并通过 cancel/resume 扩展可恢复生命周期；最终报告可直接展示相似度、共同点、差异点、参考方案、避坑提示与引用。
- `session-checkpoint:v1` 在成功 run 的同一事务保存最新公开状态；同 session 追问恢复报告、证据、路径和工具事件，失败 run 不覆盖旧快照，跨 run 同参 Action 仍会被拦截。
- `run-trace:v1` 把每次 run 的 `workflow / node / react_step / tool_call / retrieval / model_call / persistence` 七层 span 与 run 终态写在同一事务，父子关系由 `ContextVar` 推导以支持并发子调用，`GET /api/v1/runs/{run_id}/trace` 可回放单根时间轴；span 名称与属性值被限制为 ASCII 标识符，Prompt、Thought、日志原文和凭据在类型层面无法进入遥测。
- `runtime-metrics:v1` 通过 `GET /metrics` 暴露 `dataops_runs_total`、`dataops_span_count`、`dataops_span_error_count`、`dataops_span_duration_ms_sum`、`dataops_span_duration_ms_max` 五组 Prometheus 指标；聚合在数据库侧完成，因此 API/Worker 重启不会把错误率归零，runtime 未装配时返回 503 而不是全零曝光。
- `api-auth:v1` 用 ASGI 中间件按前缀 `("/api/v1", "/metrics")` 做 fail-closed 鉴权与按来源 IP 的滑动窗口限流：限流先于鉴权生效以约束令牌猜测，缺头/错 scheme/错令牌返回逐字相同的 401，令牌只以 SHA-256 摘要参与定长比较；`bearer` 缺令牌与 `disabled` 却配令牌两个方向都拒绝启动，`/health` 只公开模式与配额而从不公开令牌或其摘要。
- `run-stream:v1` 通过 `GET /api/v1/runs/{run_id}/stream` 以 SSE 增量推送 `run_snapshot` / `run_event` / `stream_end` 三种命名帧：游标写在标准 `id` 字段因此浏览器重连自动带 `Last-Event-ID`，`end_reason` 区分 run 级终态（`completed`/`failed`/`cancelled`）与连接级终止（`stream_timeout`/`run_disappeared`）；每跳先读 run 快照再读增量事件，保证 `stream_end` 之前时间线一定完整。推流只持有两个只读方法，无法推进或修改 run，轮询作为等价通道永久保留——浏览器 `EventSource` 不能携带 Authorization 头，因此 `bearer` 模式下 `/health` 的 `stream.available_under_auth` 会如实报告推流不可用。
- 双 Agent 契约是四级降级阶梯而不是装饰性审计：确定性规则与独立 Auditor 拥有**非对称**否决权（任何规则问题非空即强制 `revise`，模型的 `accept` 无法覆盖它），放行需要两层都同意；返工预算属于工作流（`max_audit_revisions` 上限为 1，模型无法扩大）且修订器只删不加；预算耗尽转安全降级；Auditor 不可用直接降级且不消耗返工预算——"审计不可用"绝不等于"审计通过"，degraded 报告永远不会进入长期记忆。同一轨迹按读者需要分四档暴露：run 结果给出完整 `AuditResult`，`run_events` 与 `/demo` 裁决卡只给稳定问题码与被否决的字段路径，trace 给出 `report.draft/audit/revise/degrade` 节点 span 与只包住模型往返的 `auditor.review` span。
- `model-transient-retry:v1` 在 Provider 外侧包一层有界指数退避重试（默认最多 2 次尝试、1s 起、上限 8s），只重复供应商已判定为瞬时的失败（429/5xx/超时/连接失败），**401/403 认证失败一次即上抛且不进入退避**；Schema 修复预算与之完全独立，refusal 不可被重发规避。重试留在包装层而不是 Provider 内部，因此具体 Provider 仍保持 `max_retries=0`、一次 `complete` 一次网络请求，"第一次 429、第二次成功"在 `model-call-metric:v1` 与 `model_call` span 里仍是两条可归因记录；启动阶段强制 `react_total_timeout_seconds` 覆盖一次重试的最坏开销，避免加了重试却在退避途中被墙钟掐断。

当前已完成全部 MCP 工具、GraphRAG 检索闭环、文档 RAG 第二知识通道、五项固定 runtime capabilities、Planner ReAct、独立 Auditor、长期案例记忆、五层小样本统一评测运行器、顶层诊断工作流、PostgreSQL Worker、run 取消/恢复、同 session 有界滚动 checkpoint、记忆删除 API、资源 API、前端 Demo 和 28 条 Golden Case 数据集。默认模型 Provider 仍为 disabled，Embedding 与 Reranker 未配置 API 时回退到确定性哈希 Provider；确定性回归与合成数据验证不冒充真实模型质量。真实模型三案例 smoke 已在固定端点实测并公开口径；复杂语义历史比较和 28 条完整真实模型 Golden 快照仍保留为明确接入点，等待固定模型与预算后再测量。详细前端信息架构、状态机和验收条件见 [`docs/frontend-design.md`](docs/frontend-design.md)。

## 本地启动

```powershell
python -m venv .venv
.venv\Scripts\python -m pip install -r requirements-dev.lock
.venv\Scripts\python -m uvicorn app.api.main:app --reload
```

访问 `http://localhost:8000/health`。

完整作品集评测需要测试数据库：

```powershell
$env:DATAOPS_TEST_DATABASE_URL='postgresql+asyncpg://...'
.venv\Scripts\python -m app.evaluation
```

无数据库快速反馈使用 `.venv\Scripts\python -m app.evaluation --skip-postgres`；其 JSON 报告会明确
`complete=false`，不能作为完整评测成绩。

真实模型 Golden 冒烟评测是单独的 opt-in 命令，不加入默认离线 Portfolio。先通过本地环境变量提供
PostgreSQL、OpenAI-compatible Provider 和本地密钥，再传入可追溯代码版本：

```powershell
$env:DATAOPS_DATABASE_URL='postgresql+asyncpg://...'
$env:DATAOPS_CHAT_PROVIDER='openai-compatible'
$env:DATAOPS_CHAT_API_KEY='仅保存在本机环境中的密钥'
.venv\Scripts\python -m app.evaluation.live_golden --code-revision '<git commit>' --output live-golden.json
```

默认只运行单组件、三组件链路、成功响应证据冲突三个代表案例。命令只有真正完成运行时才生成
`metric_kind=measured` 报告；没有密钥或数据库会在模型调用前失败。当前仓库没有伪造或占位的真实
模型分数；已发布的三案例 smoke 实测成绩、未达标项与评分口径缺陷见 [`docs/live-golden-eval-results.md`](docs/live-golden-eval-results.md)。

## Docker 启动

```powershell
Copy-Item .env.example .env
# 修改 .env 中的 DATAOPS_DB_AUTH
docker compose up --build
```

Docker 默认将 API 暴露在 `http://localhost:18000/health`；可通过
`DATAOPS_API_PORT` 修改宿主端口。容器内部端口保持为 8000。

Compose 只把 API 端口绑定到 `127.0.0.1`，因此默认 `DATAOPS_API_AUTH_MODE=disabled` 的演示实例不会
对局域网开放。要把 `/api/v1` 与 `/metrics` 暴露给其它主机，必须同时把模式改成 `bearer` 并设置至少
32 个不含空格的可见 ASCII 字符的 `DATAOPS_API_AUTH_TOKEN`——只设其中一个会拒绝启动，因为"配了令牌
但模式仍是 disabled"会让部署者误以为接口已受保护。

## 验证

```powershell
.venv\Scripts\python -m ruff check .
.venv\Scripts\python -m pytest -q
docker compose config
```

PostgreSQL 专项测试需要先启动数据库并显式选择 marker，具体命令和原理见实现指南的“测试分层”章节。

所有数据均为脱敏、合成或 Mock 内容，不接入任何生产系统、真实日志、内部域名或凭据。

## 文档与注释约束

- 人工编写的 Python、Shell、Docker、Compose、TOML 和测试文件必须说明职责、原理、边界与失败路径。
- 注释优先回答“为什么这样做”，避免把代码翻译成自然语言。
- JSON、依赖锁文件、图片和 DOCX 不支持可靠的内嵌注释，其结构、来源和生成方式由实现指南和 Schema 测试说明。
- 新增技术或改变架构时，代码、测试、实现指南和产品基线必须同步更新。
### 本切片更新：可靠 PostgreSQL Worker 与可恢复资源

资源 API 当前为 `diagnosis-resources:v4`：POST message 返回 202/queued，进程内 Worker 通过 PostgreSQL `FOR UPDATE SKIP LOCKED`、租约 heartbeat、有限重试和 session 活跃唯一索引执行任务；客户端通过 GET run/events 轮询。`POST /runs/{run_id}/cancel` 将 queued/running 原子转为可审计 cancelled，`POST /runs/{run_id}/resume` 从最新 session checkpoint 创建新 queued run；前端覆盖 queued/running/completed/failed/cancelled、409 冲突、取消/恢复和安全事件展示。
### `/demo` 学习型前端

启动 API 后可访问 `http://localhost:8000/demo`（Docker 默认端口为 `18000`）。页面展示健康状态、session/message 提交、PostgreSQL Worker 的 queued/running/terminal 状态、取消/从 checkpoint 恢复、公开 Action/Observation 时间线、审计报告和 CaseMemory 的确认/拒绝/永久删除操作。前端只渲染后端允许公开的结构化字段，不展示 Prompt、Thought、embedding、凭据或原始 Provider 响应；记忆操作始终以服务端返回状态为准。
