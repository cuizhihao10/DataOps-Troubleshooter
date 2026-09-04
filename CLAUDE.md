# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## 项目定位

面向学习与求职展示的证据驱动大数据排障 Agent。用户描述 LTS（调度）/ BDS（计算）/ FlashSync（同步）三个模拟组件的故障后，系统通过真实 MCP 协议收集证据、GraphRAG 溯源、双 Agent（Planner ReAct + 独立 Auditor）产出带引用的结构化报告，并把审计通过的案例写入长期记忆。

全部数据为脱敏、合成或 Mock，不接入任何生产系统、真实日志、内部域名或凭据。

## 先读这三份文件

1. `AGENTS.md` — 仓库级硬性规则与冲突优先级。
2. `.agents/skills/build-dataops-troubleshooter/SKILL.md` — 不可删减能力、架构边界、垂直切片工作法。
3. `docs/implementation-guide.md`（约 1400 行，第 1 节是阅读路径）— 每项技术的原理、调用链、限制与验证命令。

事实来源优先级：当前用户要求 > `docs/product-design.md` > 已有测试与对外接口契约 > 当前代码 > SKILL 默认约束。改 Prompt 或结构化输出 Schema 前先读 `docs/prompt-contracts.md`；调整依赖、目录、检索流程或开发顺序前先读 `docs/reference-adoption.md`。

## 常用命令

Windows 仓库，bash 下用 `.venv/Scripts/python`。

```bash
.venv/Scripts/python -m pip install -r requirements-dev.lock

# 本地运行；访问 /health 与 /demo
.venv/Scripts/python -m uvicorn app.api.main:app --reload

# 静态检查（ruff，line-length 100，规则集 E/F/I/UP/B/ASYNC）
.venv/Scripts/python -m ruff check .

# 快速测试。pyproject 的 addopts 已包含 -m 'not postgres'，默认跳过需要数据库的用例
.venv/Scripts/python -m pytest -q

# 单文件 / 单用例
.venv/Scripts/python -m pytest -q tests/unit/test_react_loop.py
.venv/Scripts/python -m pytest -q tests/unit/test_react_loop.py::test_name

# PostgreSQL 集成测试必须显式选 marker 并提供测试库
DATAOPS_TEST_DATABASE_URL='postgresql+asyncpg://...' .venv/Scripts/python -m pytest -m postgres

# 五层作品集评测（20 个指标，portfolio-eval-run:v23）
DATAOPS_TEST_DATABASE_URL='postgresql+asyncpg://...' .venv/Scripts/python -m app.evaluation
.venv/Scripts/python -m app.evaluation --skip-postgres   # 无库快速反馈，报告 complete=false

# 真实模型冒烟评测（opt-in，需要 DATAOPS_CHAT_PROVIDER/DATAOPS_CHAT_API_KEY/DATAOPS_DATABASE_URL）
.venv/Scripts/python -m app.evaluation.live_golden --code-revision <git-sha> --output live-golden.json

# Docker：宿主 18000 → 容器 8000；先 cp .env.example .env 并设置 DATAOPS_DB_AUTH 与 DATAOPS_MCP_AUTH
# compose 起三个服务：database、mcp-gateway（Streamable HTTP MCP 网关，不发布宿主端口）、api
docker compose up --build
# 只校验配置，必须带 --quiet：不带它会把插值后的完整配置（含 .env 里的 key 与口令）打到 stdout
docker compose config --quiet
```

## 架构：三层嵌套 LangGraph

核心是三个真实 `StateGraph`，外层复用内层，不是一个扁平函数链。改动编排时要判断该落在哪一层。

```
AuditedDiagnosisWorkflow (audited-diagnosis-workflow:v2, app/orchestration/diagnosis_workflow.py)
  recall_case_memories → run_react → explain_case_matches → run_report → stage_case_memory
    │                                   │
    │  run_react 内嵌：BoundedReactLoop (langgraph-react-loop:v3, react_loop.py)
    │    select_capabilities → planner_react ⇄ execute_tools（条件边，有界循环，单轮可并行 1–3 个 Action）
    │
    └─ run_report 内嵌：AuditedReportWorkflow (audited-report-workflow:v2, report_workflow.py)
         draft_report → audit_report →（revise_report ⇄ audit_report | degrade_report | accept）
```

- **历史召回只在 trigger 命中时执行**，同一批 confirmed 案例同时进入 Planner 与 Auditor，`explain_case_matches` 只做确定性解释、不重新搜索。
- **`stage_case_memory` 必须在审计之后**，且只接收 accepted 且含根因的报告；候选默认 pending，等用户 confirm。
- **实时 Observation 永远优先于历史案例**。历史只能作参考证据，冲突时报告要突出差异。

## 其余关键边界

- **MCP 是真协议边界，传输由 `mcp-transport:v1` 选定**：生产路径是 Streamable HTTP —— `app/mcp/streamable_http.py` 打一个独立部署的网关（compose 的 `mcp-gateway` service），`mcp_server/security.py` 复用 `ApiSecurityGuard` 做 fail-closed 鉴权，缺令牌拒绝启动，401/403 归 `PERMISSION_DENIED`（不在可重试集合内）。stdio（`app/mcp/client.py` 用 `sys.executable -m mcp_server.server` 起子进程）保留为可选配置与代码学习路线，**不再新增功能或测试**。执行器只依赖 `app/mcp/protocol.py` 的 `McpToolClient` Protocol，新传输不改上层。禁止在 Agent 节点里直接读 Fixture 冒充工具调用。九个只读工具名以 `app/domain/tooling.py` 的 `ToolName` 为准，不得改名、合并或删减。瞬时错误最多重试一次，且重试不增加 `react_step`。
- **并行只买延迟，不买预算**：Planner 单轮可提交 1–`max_parallel_tool_actions`（默认 3）个互不依赖的只读 Action，`execute_tools` 用 `asyncio.gather` 并发执行（两种传输都不共享会话状态：HTTP 下共享 httpx 连接池但每次 `call_tool` 新建 MCP 会话，stdio 下每次 `call_tool` 起独立子进程，所以并发安全）。一批 N 个 Action 记 N 步，因此调大并行度不会让模型多看证据。所有门禁（并行上限 → 步数预算 → capability 范围 / `trace_id` / 指纹去重）都整批拒绝而不截断，否则 Planner 会基于"其余调用也发生了"的错误前提继续推理。Prompt 里的批次上限与门禁同源：控制器注入 `min(配置并行度, 剩余步数)`。
- **PostgreSQL 是唯一状态服务**：业务表、pgvector 向量、显式图节点/边表全在里面。不要引入 Neo4j / Redis / 独立向量库。
- **`app/capabilities/` 是配置不是 Agent**：五项固定能力只输出 Prompt 片段、工具优先级、输入要求和输出校验规则，不能自己调 LLM/MCP/检索。别和 `.agents/skills/` 的开发 Skill 混淆。
- **模型默认 disabled**：`DATAOPS_CHAT_PROVIDER=disabled`。诊断资源 API 要求 PostgreSQL + Planner + Auditor 三者齐备才发布 runtime，否则明确返回 503（不静默降级）。
- **run 由 PostgreSQL Worker 执行**：POST message 返回 202/queued，Worker 用 `FOR UPDATE SKIP LOCKED` + 租约 heartbeat 领取；同 session 活跃 run 唯一（冲突 409）。cancel/resume 走 `session_checkpoints` 快照，失败或取消的 run 不覆盖旧快照。
- **绝不外泄推理过程**：API 响应、run_events、`run-trace:v1` span、`run-stream:v1` SSE 帧、`/metrics` 曝光和 `/demo` 前端都不得包含 Thought、原始思维链、Prompt、embedding、凭据或 Provider 原始响应。span 名称与属性值被正则限制为 ASCII 标识符（空格与 CJK 直接拒绝），这是结构性保证而不是纪律要求。前端也不用不可信数据赋值 `innerHTML`。
- **可观测性有两个出口**：`model-call-metric:v1` 是进程内 ContextVar recorder，只服务离线评测；`run-trace:v1` 的 span 与 run 终态同事务落 `run_trace_spans`，经 `GET /api/v1/runs/{run_id}/trace` 回放，`runtime-metrics:v1` 经 `GET /metrics` 曝光五组指标。指标聚合必须放在数据库（进程重启不能让错误率归零），runtime 未装配时两者都返回 503 而不是全零。
- **推流是读法不是执行路径**：`run-stream:v1`（`app/api/streaming.py`）的 `iter_run_stream` 只持有 `RunStreamSource` 的两个只读方法，结构上无法推进或修改 run；run 永远由 Worker 执行，轮询是永久保留的等价回退。每跳必须先读 run 快照再读增量事件（`complete_run` 同事务提交终态与最后一批事件，反序会漏发事件）。`end_reason` 必须区分 run 级终态与连接级终止（`stream_timeout`/`run_disappeared`）。游标走标准 SSE `id` 字段以复用 `Last-Event-ID`，不要自造续传协议。浏览器 `EventSource` 不能带请求头，因此 bearer 模式下推流必然 401，这是已声明的限制，由 `/health` 的 `stream.available_under_auth` 公开。
- **鉴权与限流是 fail-closed 前缀边界**：`api-auth:v1`（`app/api/security.py`）用一个 ASGI 中间件保护 `("/api/v1", "/metrics")`，新增 `/api/v1/...` 路由默认已受保护，不要改成逐路由 `Depends`。限流必须先于鉴权（否则猜令牌不受配额约束），三种失败共享逐字相同的 401，令牌只以 SHA-256 摘要定长比较；`bearer` 缺令牌与 `disabled` 却配令牌都在 lifespan 阶段拒绝启动。`/health` 与 `/demo` 保持公开。
- **审计是四级阶梯，规则对模型有非对称否决权**：确定性规则（`app/reporting/policy.py`）问题非空即强制 `revise`，模型的 `accept` 覆盖不了它；放行要两层都同意。返工预算属于工作流（`max_audit_revisions` ≤ 1，模型无法扩大），`SafeReportReviser` 只删不加；预算耗尽转 `degraded`；`AuditorAgentError` 立即 `degraded` 且**不消耗返工预算、不重跑报告**——"审计不可用"不等于"审计通过"。返工必须把 `audit_result` 置空并重新跑一遍规则校验，不能复用首轮 issues。路由只读 `AuditStatus` 与 `retry_count`，永不解析 `revision_instructions`。`run_events` 与 `/demo` 裁决卡只暴露问题码与 `claim_path`，不暴露 `issues[].message`。
- **工具只读**：不实现自动重跑、扩容、删表或同步修复。

## 两个容易踩的门禁

**1. 契约 ID 必须多处同步。** 所有版本化契约（`planner-react:v8`、`langgraph-react-loop:v3`、`case-memory:v2`、`diagnosis-resources:v4`、`api-auth:v1`、`run-stream:v1`、`mcp-transport:v1` 等）在 `app/core/settings.py` 有一份期望值，模块里有一份常量，`app/api/main.py` 的 lifespan 逐项比对，不一致就拒绝启动。升版本要同时改：settings 默认值、模块常量、`docs/prompt-contracts.md`、`docs/implementation-guide.md`，以及 `tests/unit/test_documentation_policy.py` 里对应的字面量断言。

**2. `tests/unit/test_documentation_policy.py` 是硬门禁**，AST + tokenize 扫描 `app/`、`mcp_server/`、`tests/`：

- 每个模块的 docstring ≥ 60 字符；
- 每个 class / def / async def（含方法、嵌套函数、测试函数）的 docstring ≥ 80 字符，文件头说明不能替代；
- 该文件 `CRITICAL_INLINE_COMMENT_FILES` 列出的边界模块至少 2 条真实内联注释——新增此类模块时要把它加进列表；
- 断言 `docs/*.md` 和 DOCX 里存在大量精确字面量（章节名、指标数字、契约 ID）。**改了实测数字或版本号就必须同步文档，否则这个测试会失败。**

注释要解释"为什么这样设计"和"边界如何保证"，不要逐行翻译代码。

## 评测数字的诚实性要求

评测分五层：GraphRAG 消融、记忆召回消融、历史影响消融、Auditor 影响消融、Golden 诊断基线。当前的满分/增益全部来自**确定性脚本替身**的小样本，报告里必须保留"不能外推为真实 LLM"的限制说明。任何数字都要标注实测值或目标值；没有真实模型评测结果时，不得在 README、产品文档或简历里宣称提升百分比。

## 数据库迁移

Alembic 链是线性单 head：`0001 → 0002 → 0003 → 0004 → 20260716_0005(session_checkpoints) → 20260716_0006(diagnosis_worker) → 20260716_0007 → 20260716_0008(documents) → 20260716_0009(run_trace_spans) → 20260716_0010(remediation_risk_level)`。注意 `20260716_0005_diagnosis_worker.py` 的文件名与它内部的 `revision = "20260716_0006"` 不一致，按 revision 值而不是文件名判断顺序。Docker 启动命令自带 `alembic upgrade head && python -m app.persistence.seed`。

