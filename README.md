# DataOps Troubleshooter

面向公开作品集的、证据驱动的大数据链路智能排障 Agent。当前已经完成领域契约、九工具真实 MCP 边界，以及 GraphRAG 的 PostgreSQL 图存储、全文种子召回和两跳路径扩展首切片。

本项目同时是学习与求职展示项目。代码中的模块级 docstring 和关键注释负责解释局部设计，完整技术原理、数据流、设计取舍和验证方法统一维护在 [`docs/implementation-guide.md`](docs/implementation-guide.md)。

## 当前切片

- 固定产品设计中的 9 个只读 MCP 工具名称。
- 使用 Pydantic 定义 Planner 决策、MCP 请求/响应、Evidence、AgentState、报告和案例记忆契约。
- 提供 5 个脱敏且确定性的合成场景，以及对应 Golden Case 格式。
- 启动时校验全部 Fixture 和 Golden Case 引用。
- 提供 `GET /health`，返回契约版本、运行预算和已加载场景。
- 通过官方 MCP Python SDK 和 stdio 协议暴露产品规定的 9 个只读工具，并将返回标准化为 Evidence 与 ToolEvent。
- 瞬时错误最多自动重试一次，每次尝试均保留独立 ToolEvent；空结果和权限错误不会重试。
- PostgreSQL + pgvector 保存显式知识节点和关系边，并支持全文种子召回与 1–2 跳路径扩展。

当前已完成全部 MCP 工具，以及 GraphRAG 的 PostgreSQL 图存储、人工知识种子、全文种子召回和路径扩展首切片。pgvector 语义召回、混合评分、LangGraph ReAct、Planner/Auditor 和长期记忆仍待后续实现。

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
