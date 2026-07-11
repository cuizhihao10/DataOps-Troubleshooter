# DataOps Troubleshooter

面向公开作品集的、证据驱动的大数据链路智能排障 Agent。当前已经完成领域契约、九工具真实 MCP、GraphRAG、五项固定 capability、LangGraph 有界循环，以及 Planner/Auditor OpenAI-compatible Structured Outputs。

本项目同时是学习与求职展示项目。代码中的模块级说明、每个 callable 的详细 docstring 和复杂函数关键步骤注释负责解释局部设计，完整技术原理、数据流、设计取舍和验证方法统一维护在 [`docs/implementation-guide.md`](docs/implementation-guide.md)。

## 当前切片

- 固定产品设计中的 9 个只读 MCP 工具名称。
- 使用 Pydantic 定义 Planner 决策、MCP 请求/响应、Evidence、AgentState、报告和案例记忆契约。
- 提供 5 个脱敏且确定性的合成场景，以及对应 Golden Case 格式。
- 启动时校验全部 Fixture 和 Golden Case 引用。
- 提供 `GET /health`，返回契约版本、运行预算和已加载场景。
- 通过官方 MCP Python SDK 和 stdio 协议暴露产品规定的 9 个只读工具，并将返回标准化为 Evidence 与 ToolEvent。
- 瞬时错误最多自动重试一次，每次尝试均保留独立 ToolEvent；空结果和权限错误不会重试。
- PostgreSQL + pgvector 保存显式知识节点、关系边和带 Provider 溯源的向量，支持全文/向量混合召回、五项可解释评分与 1–2 跳路径扩展。
- Evidence Bundle 按 UTF-8 JSON 字节、节点数和路径数三重预算原子选择证据，并返回稳定 `kn_*` / `path_*` 引用与 omitted IDs。
- 版本控制的消融案例真实比较 vector-only 与 vector+graph；当前实测根因命中持平，必要因果链完整率由 0.0 提升至 1.0。
- 五项 capability 以 `runtime-capabilities:v1` 输出 Prompt 片段、工具优先级、输入要求和输出规则；历史匹配仅按需启用，实时 Observation 始终优先。
- `langgraph-react-loop:v1` 真实执行 capability 注入、Planner 决策、MCP Action、Observation 回写和回到 Planner，并拦截重复 Action、组件越界、无效引用与 trace 漂移。
- `planner-react:v2` 将 system 规则与 user 运行数据隔离；官方异步 SDK 从 Pydantic 提交 strict Schema，首次无效输出最多修复一次，refusal 和 Provider 错误安全停止。
- 确定性 Builder 只把有有效支持引用且无反对证据的假设提升为根因；链路和建议分别引用 `path_id` 与知识节点证据。
- `auditor-report:v1` 使用独立 Structured Outputs Agent；确定性问题可否决错误 accept，`audited-report-workflow:v1` 最多返工一次，二次未通过或 Provider 不可用时返回安全降级报告。

当前已完成全部 MCP 工具、GraphRAG 检索闭环、五项固定 runtime capabilities、Planner ReAct 控制器、结构化报告草稿和独立 Auditor 返工协议。默认模型 Provider 仍为 disabled，自动化测试使用真实 SDK + MockTransport，不宣称已经调用付费模型或取得模型质量成绩。长期记忆、完整诊断 API 和更多 Golden Case 仍待后续实现。

## 本地启动

```powershell
python -m venv .venv
.venv\Scripts\python -m pip install -r requirements-dev.lock
.venv\Scripts\python -m uvicorn app.api.main:app --reload
```

访问 `http://localhost:8000/health`。

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
