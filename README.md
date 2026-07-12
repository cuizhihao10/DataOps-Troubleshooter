# DataOps Troubleshooter

面向公开作品集的、证据驱动的大数据链路智能排障 Agent。当前已经完成领域契约、九工具真实 MCP、GraphRAG、五项固定 capability、LangGraph 有界循环、Planner/Auditor OpenAI-compatible Structured Outputs，以及受控长期案例记忆。

本项目同时是学习与求职展示项目。代码中的模块级说明、每个 callable 的详细 docstring 和复杂函数关键步骤注释负责解释局部设计，完整技术原理、数据流、设计取舍和验证方法统一维护在 [`docs/implementation-guide.md`](docs/implementation-guide.md)。

## 当前切片

- 固定产品设计中的 9 个只读 MCP 工具名称。
- 使用 Pydantic 定义 Planner 决策、MCP 请求/响应、Evidence、AgentState、报告和案例记忆契约。
- 提供 6 个脱敏且确定性的合成场景，以及对应 Golden Case 格式。
- 启动时校验全部 Fixture 和 Golden Case 引用。
- 提供 `GET /health`，返回契约版本、运行预算和已加载场景。
- 通过官方 MCP Python SDK 和 stdio 协议暴露产品规定的 9 个只读工具，并将返回标准化为 Evidence 与 ToolEvent。
- 瞬时错误最多自动重试一次，每次尝试均保留独立 ToolEvent；空结果和权限错误不会重试。
- PostgreSQL + pgvector 保存显式知识节点、关系边和带 Provider 溯源的向量，支持全文/向量混合召回、五项可解释评分与 1–2 跳路径扩展。
- Evidence Bundle 按 UTF-8 JSON 字节、节点数和路径数三重预算原子选择证据，并返回稳定 `kn_*` / `path_*` 引用与 omitted IDs。
- 版本控制的消融案例真实比较 vector-only 与 vector+graph；当前实测根因命中持平，必要因果链完整率由 0.0 提升至 1.0。
- 五项 capability 以 `runtime-capabilities:v1` 输出 Prompt 片段、工具优先级、输入要求和输出规则；历史匹配仅按需启用，实时 Observation 始终优先。
- `langgraph-react-loop:v2` 真实执行 capability 注入、Planner 决策、MCP Action、Observation 回写和回到 Planner，并把 raw confirmed 案例与确定性解释绑定后注入 Planner。
- `planner-react:v4` 隔离 system/user 数据，并显式注入同会话上一轮报告及历史案例共同点、差异点、参考动作和避坑提示；Structured Outputs 仍只返回结构化 Action。
- 确定性 Builder 只把有有效支持引用且无反对证据的假设提升为根因；链路和建议分别引用 `path_id` 与知识节点证据。
- `auditor-report:v2` 使用独立 Structured Outputs Agent 审核实时事实与历史解释冲突；`audited-report-workflow:v2` 的确定性问题可否决错误 accept，最多返工一次。
- `case-memory:v2` 只接收 Auditor accepted 且含根因的报告，新候选默认为 pending；exact signature 优先、pgvector cosine 次之，同 run 重放不会重复增加 occurrence。
- `POST /api/v1/memories/{memory_id}/confirm` 支持 confirm、reject 和重新 confirm；`GET /api/v1/memories/search` 只返回 confirmed 案例，数据库未启用时明确返回 503。
- confirmed 案例在同一事务注册为 GraphRAG `case` 节点，复用记忆 embedding，并按独立阈值建立稳定双向 `SIMILAR_TO`；reject 删除节点并级联清边。
- 历史召回合并 pgvector 直接 top-k 与 `SIMILAR_TO` 图邻居，公开 vector/graph 通道、直接相似度、图传播分和稳定 edge 引用。
- `memory-recall-eval:v1` 使用 3 条合成查询真实比较 vector-only/vector+graph；当前小样本 Macro Recall@K 与 Precision@K 实测从 0.8333 变为 1.0000，禁止案例命中为 0。
- `history-impact-eval:v1` 使用 3 条合成诊断真实比较 Memory off/on；确定性 LangGraph 小样本中必要 Action 覆盖实测从 0.6667 变为 1.0000，意外 Action 率从 0.3333 降为 0，根因命中、实时引用、历史投影和冲突保护均保持 1.0000。
- `auditor-impact-eval:v1` 使用 3 条语义缺陷案例比较规则对照与完整 Auditor；预期问题发现率实测从 0 变为 1.0000，危险内容残留率从 1.0000 降为 0，安全处置率从 0 变为 1.0000，其中两例修订后接受、一例持续冲突后降级。
- `golden-case:v7` 同时覆盖零工具补参、部分证据和 LTS 三类观察全部不可用的安全降级；当前 18 条案例使用 8 个脱敏 Fixture。
- `golden-diagnosis-eval:v11` 要求完整保留空结果与瞬时失败边界，但在没有实时 Evidence 时不输出根因；当前 18/28 确定性脚本满分不冒充真实 LLM 成绩。
- `portfolio-eval-run:v12` 通过 `python -m app.evaluation` 一次执行五层、19 个独立指标。
- `audited-diagnosis-workflow:v2` 按 history trigger 召回 confirmed 案例，在 ReAct 前后两次确定性比较同批候选，再串联独立 Auditor 和审计后 memory staging。
- `diagnosis-resources:v2` 提供 session/message/run/event PostgreSQL 资源；最终报告可直接展示相似度、共同点、差异点、参考方案、避坑提示与引用。
- `session-checkpoint:v1` 在成功 run 的同一事务保存最新公开状态；同 session 追问恢复报告、证据、路径和工具事件，失败 run 不覆盖旧快照，跨 run 同参 Action 仍会被拦截。

当前已完成全部 MCP 工具、GraphRAG 检索闭环、五项固定 runtime capabilities、Planner ReAct、独立 Auditor、长期案例记忆、五层小样本统一评测运行器、顶层诊断工作流、资源 API 和同 session checkpoint 追问恢复。默认模型 Provider 仍为 disabled；Golden 诊断层是 18 条确定性脚本回归基线，自动化测试不宣称已经调用付费模型或取得模型质量成绩。可靠后台 worker、LangGraph 逐节点中断恢复、模型级复杂语义对比和完整 28 条 Golden Case 仍待后续实现。

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

## Docker 启动

```powershell
Copy-Item .env.example .env
# 修改 .env 中的 DATAOPS_DB_AUTH
docker compose up --build
```

Docker 默认将 API 暴露在 `http://localhost:18000/health`；可通过
`DATAOPS_API_PORT` 修改宿主端口。容器内部端口保持为 8000。

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
