---
name: build-dataops-troubleshooter
description: "Implement, refactor, test, evaluate, or plan the DataOps Troubleshooter portfolio project: a lightweight Python agent application for LTS/BDS/FlashSync fault diagnosis using a bounded Planner ReAct loop, a separate Auditor agent, MCP mock tools, PostgreSQL plus pgvector GraphRAG, audited long-term case memory, evidence citations, FastAPI, and LangGraph. Use when Codex is asked to vibe-code features, fix bugs, design APIs or schemas, add prompts, ReAct behavior, tools, retrieval or memory, create tests and evaluation fixtures, or prepare a runnable demo for this repository."
---

# Build DataOps Troubleshooter

## 1. 守住项目定位

将项目实现为一个可运行、可解释、可评测的 AI Agent 求职作品，而不是通用 DataOps 平台。

围绕一个主问题开发：用户提供大数据任务告警或故障描述后，系统跨 LTS 调度、BDS 计算、FlashSync 同步三个模拟组件收集证据，定位故障链路，输出有引用、风险说明和不确定性声明的排障报告。

始终使用脱敏、合成或 Mock 数据。不得接入原单位生产系统、真实日志、内部域名、凭据或未公开接口。

## 2. 保持不可删减的核心能力

实现并保留以下能力，不得用普通单轮聊天或静态规则替代：

1. **ReAct 行为闭环**：让 Planner 按隐藏式 Reason、结构化 Action、标准化 Observation 循环调查，并在明确停止条件下结束。
2. **多 Agent 协作**：使用 Planner ReAct Agent 和 Auditor Agent 两个独立角色节点。
3. **GraphRAG**：先做语义种子召回，再沿显式实体关系扩展并返回可追溯路径。
4. **长期记忆**：将通过审计的故障案例结构化、去重并持久化，供后续相似故障召回。
5. **历史案例匹配**：在推理阶段按需召回已确认的同类故障，返回共同点、差异点、参考方案和避坑提示。
6. **MCP 工具调用**：通过真实 MCP 协议访问本地 Mock 工具服务，不得在 Agent 节点中直接读取 Fixture 冒充工具调用。
7. **证据约束**：让每项根因与建议引用工具结果、知识节点、图路径或已确认历史案例。

## 3. 控制范围

优先完成以下主流程：

- 单组件故障诊断。
- LTS → BDS → FlashSync 跨组件链路溯源。
- 基于同一会话的追问与方案细化。
- 相似历史案例召回。
- 审计、结构化报告和案例记忆写入。

将全链路批量巡检、告警订阅、自动修复、复杂权限、多租户、通用工作流编排、知识自动爬取和完整前端平台放在核心范围之外。只有在主流程通过测试后，才增加明确获批的扩展。

固定两个 LLM Agent。将输入校验、路由、检索、记忆写入、报告渲染等做成确定性节点或服务，不要为每个步骤新建 Agent。

不得把 ReAct 简化为提示词中的一个名词。主流程必须真实执行 Planner → Action → MCP 工具 → Observation → Planner 的有界循环。

## 4. 遵循事实来源顺序

将 `docs/product-design.md` 作为本仓库已批准、供编码检索的产品设计基线；`docs/DataOps_Troubleshooter_产品设计文档_v2.0.docx` 是内容一致的正式阅读版。涉及需求、架构、接口、里程碑或验收口径时，先读取 Markdown 中的相关章节。若两份文档出现内容差异，先同步文档，不要静默选择其中一份继续开发。

涉及 Planner ReAct Prompt、GraphRAG 实体/关系抽取、结构化输出或 Prompt 评测时，必须读取并遵守 `docs/prompt-contracts.md`。不要在代码中另建与该文件冲突的 Prompt Schema。

涉及依赖、目录、工具数量、检索链路、运行时 capabilities、记忆写入、API 或开发顺序时，读取 `docs/reference-adoption.md`，按其中“采用 / 调整 / 暂缓”的决策实施。

按以下优先级处理冲突：

1. 当前用户明确要求。
2. 已批准的产品设计文档。
3. 现有测试与对外接口契约。
4. 当前代码实现。
5. 本技能中的默认约束。

先检查仓库、产品文档、`pyproject.toml`、环境示例和测试，再做设计判断。不要凭空假设文件、依赖、接口或数据库表已经存在。

## 5. 维持轻量架构

优先采用以下边界；若仓库已有等价结构，延续现有结构并避免无意义迁移：

```text
app/
  api/                 # FastAPI 入口、路由与响应模型
  agents/              # Planner ReAct、Auditor 节点与 Prompt
  capabilities/        # 固定领域能力配置，不是 Codex Skill
  orchestration/       # state、react_loop、LangGraph workflow
  mcp/                 # MCP 客户端、白名单与 Observation 适配
  retrieval/           # vector、graph、hybrid 检索
  memory/              # short_term 与 long_term 记忆
  domain/              # Evidence、Action、Hypothesis、Report 模型
  persistence/         # PostgreSQL/pgvector 仓储与迁移
  core/                # settings 与依赖注入
  observability/       # 结构化日志、运行轨迹与指标
mcp_server/tools/      # LTS/BDS/FlashSync Mock MCP 工具
data/
  fixtures/            # 可复现工具响应与异常场景
  knowledge/           # 脱敏知识种子、SOP 和案例
tests/
  unit/
  integration/
  evals/
pyproject.toml
README.md
```

在 `app/capabilities/` 中固定实现单组件诊断、跨组件链路溯源、历史案例匹配、风险评估和结构化报告五类领域能力。让能力只提供 Prompt 片段、工具优先级、输入要求和输出校验；通过轻量 registry 按需选择组合，不要让能力自行调用 LLM、复制工作流或演变成通用插件系统。不要将运行时 capabilities 与 `.agents/skills/` 中的 Codex 开发 Skill 混淆。

使用 Python 3.11+、FastAPI、LangGraph、ReAct 结构化决策、官方 MCP Python SDK 或 FastMCP、PostgreSQL 和 pgvector。通过一个 OpenAI-compatible LLM 适配层隔离模型供应商。避免同时引入多个 Agent 框架、向量数据库或图数据库。

让 PostgreSQL 同时保存业务状态、向量和图关系。除非已有验证过的需求，不要增加 Neo4j、Redis、Kafka 或独立搜索集群。

### 5.1 建立依赖基线

使用 `pyproject.toml` 管理依赖并生成锁文件，不直接复制参考材料中的旧版本号。初始化时至少评估以下依赖：

- 运行时：`fastapi`、`uvicorn`、`pydantic`、`pydantic-settings`、`sqlalchemy`、`asyncpg`、`alembic`、`pgvector`、`langgraph`、官方 `mcp` SDK、`httpx`。
- 模型适配：OpenAI-compatible SDK；只有实际使用 LangChain 适配器时才加入 `langchain-core` / `langchain-openai`。
- 测试与质量：`pytest`、`pytest-asyncio`、HTTP/MCP mock 工具、`ruff`，并按仓库实际需要选择类型检查器。

保持异步 I/O。不要同时保留 `psycopg2` 与 `asyncpg` 两套数据库路径，不要因为示例依赖列表而引入未使用的 `numpy` 或框架包。

### 5.2 按依赖顺序实施

按以下顺序推进，但每个阶段仍以可验证垂直切片交付：

1. **契约与 Fixture**：建立领域模型、配置、错误码、Prompt Schema、3–5 个合成场景和测试骨架。
2. **MCP 工具层**：实现产品文档定义的 9 个只读工具、统一输入输出、正常/异常 Mock 和独立单测。
3. **混合检索与 GraphRAG**：实现 pgvector 语义召回、PostgreSQL 全文召回、合并去重、节点/边扩展和路径证据；使用脱敏种子验证。
4. **运行时 capabilities**：实现五类固定能力及 registry，输出 Prompt 片段、工具优先级和校验规则。
5. **LangGraph ReAct 双 Agent**：贯通路由、检索、Planner ReAct、MCP Action / Observation、草稿、Auditor 和返工。
6. **记忆与 API**：实现 checkpoint、长期记忆候选/确认/去重/召回，以及会话、运行事件和记忆接口。
7. **评测与作品集**：完成 Golden Cases、异常降级、消融测试、README、架构图、演示脚本和实测结果记录。

不要为了匹配阶段名称一次性生成所有文件。每一步只创建当前闭环确实需要且能够测试的实现。

## 6. 固定 Agent 工作流

按以下状态图实现主流程：

```text
validate_input
  -> load_session
  -> retrieve_context
  -> planner_react
  -> execute_mcp_tools
  -> record_observation
  -> planner_react (最多循环到 ReAct 预算)
  -> draft_report
  -> auditor
       -> revise_once -> planner_react/draft_report
       -> accept
  -> stage_case_memory
  -> render_response
```

让 Planner 的每轮 ReAct 决策遵循以下契约：

- **Reason**：允许模型在内部分析，但只输出简短 `decision_summary`、假设更新和证据缺口。不得请求、记录或展示逐步 `Thought` 或原始思维链。
- **Action**：返回 `call_tool`、`finish` 或 `need_user_input`；调用工具时必须提供白名单工具名和通过 Pydantic 校验的参数。
- **Observation**：由确定性工具节点根据真实 MCP 返回生成 `Evidence` 与 `ToolEvent`。不得让模型自行填写或改写 Observation。

结构化决策至少包含 `status`、`decision_summary`、`hypothesis_updates`、`action`、`evidence_refs` 和 `stop_reason`。默认最多执行 6 步 ReAct 工具行动；当证据充分、需要用户补参、继续行动无信息增益、预算耗尽或总超时时停止。除上一步为允许重试的瞬时错误外，拦截同一工具和参数的重复 Action。

让 Planner Agent：

- 识别当前故障对象与缺失信息。
- 生成短计划和待验证假设。
- 从允许列表中生成单个结构化 Action。
- 根据标准化 Observation 更新假设并决定继续或停止。
- 汇总故障链路、根因、建议和证据引用。

让 Auditor Agent：

- 检查每项关键结论是否有有效 `evidence_id`。
- 检查引用内容是否真的支持结论。
- 检查工具结果、GraphRAG 路径和历史案例是否互相矛盾。
- 检查修复建议是否包含风险等级、前置条件和回滚提示。
- 返回 `accept` 或结构化的 `revise` 指令，不自行编造新事实。

限制单次运行的 ReAct 步数、工具调用次数、图扩展跳数、总超时和审计重试次数。默认最多 6 步工具行动、2 跳图扩展、1 次审计返工；通过配置覆盖，不要散落魔法数字。

不要向用户展示模型原始思维链。只展示短计划、Action / Observation 时间线、停止原因、证据引用和最终结论。

## 7. 统一状态和领域模型

用 Pydantic 定义共享模型，至少覆盖：

- `AgentState`：`run_id`、`session_id`、`user_query`、`intent`、`active_capabilities`、`plan`、`hypotheses`、`evidence`、`tool_events`、`retrieved_paths`、`react_step`、`next_action`、`observation_refs`、`stop_reason`、`draft_report`、`audit_result`、`retry_count`。不要加入原始 `reasoning_process` 或完整 `Thought`。
- `Evidence`：`evidence_id`、`source_type`、`source_id`、`content`、`observed_at`、`reliability`、`metadata`。
- `FaultHypothesis`：现象、候选根因、涉及组件、支持/反对证据、状态和置信度。
- `DiagnosisReport`：摘要、故障链路、根因、证据、修复步骤、风险、不确定性、相似案例。
- `CaseMemory`：症状、根因、故障路径、解决方案、组件、标签、证据引用、确认状态、出现次数和时间戳。

在边界处校验模型，不要在节点之间传递松散字典。让所有 ID 可序列化、可记录、可测试。

## 8. 实现真实的 MCP 工具层

在一个本地 MCP Mock 服务中暴露最小工具集：

- `lts.get_task_status`
- `lts.get_task_log`
- `lts.get_dependency_topology`
- `bds.get_task_status`
- `bds.get_task_log`
- `bds.get_table_info`
- `flashsync.get_sync_delay`
- `flashsync.get_sync_log`
- `flashsync.check_consistency`

统一工具输入：资源标识、时间范围、`scenario_id` 和 `trace_id`。统一工具输出：`ok`、`data`、`evidence`、`error_code`、`error_message`、`observed_at`。

让 Fixture 以 `scenario_id` 驱动确定性响应，使测试和演示可重复。覆盖成功、超时、空结果、权限拒绝和服务异常。不要让 Mock 永远返回成功。

将每次 MCP 返回标准化为 ReAct Observation。仅对可重试的瞬时错误重试一次；仍失败时允许使用知识库生成低置信度参考和下一步检查建议，但不得声称实时工具已经确认根因。

保持工具只读。不得实现生产写操作、自动重跑、扩容、删表或同步修复。

## 9. 实现轻量 GraphRAG

使用显式节点表和边表，不要把关系只写进自然语言：

- 节点类型：`component`、`task`、`dataset`、`symptom`、`root_cause`、`solution`、`case`、`sop`。
- 关系类型：`RUNS_ON`、`DEPENDS_ON`、`PRODUCES`、`CONSUMES`、`MANIFESTS_AS`、`CAUSED_BY`、`RESOLVED_BY`、`SIMILAR_TO`。

按以下检索步骤执行：

1. 对问题和当前工具观察生成检索查询。
2. 使用 pgvector 语义检索和 PostgreSQL 全文检索分别召回 top-k 种子节点。
3. 合并去重两路种子，并仅沿允许的关系类型扩展 1–2 跳。
4. 按语义相似度、全文命中、路径相关性、证据可靠性和案例新鲜度组合评分。
5. 返回节点内容、完整路径、分数和来源，形成受上下文预算约束的 `evidence_bundle`。

为跨组件用例验证图路径确实参与答案。增加向量-only 与 vector+graph 的消融测试；不得仅把 “GraphRAG” 写进类名或提示词。

先使用人工整理的脱敏 JSON/YAML 知识种子。不要在首版加入复杂的自动实体抽取、社区发现或大规模离线索引流水线。

## 10. 实现受控长期记忆

将短期会话状态与长期案例记忆分开：

- 使用 LangGraph checkpoint 或会话仓储保存短期上下文。
- 使用 `case_memories` 保存跨会话的已确认案例。

只在 Auditor 通过后生成记忆候选。默认先暂存，再由用户确认或明确的测试配置自动确认。禁止把未经审计的模型输出直接写入长期知识库。

写入前执行去重：结合组件/根因签名与向量相似度判断重复；命中重复案例时更新出现次数、最近时间和新增证据，不要无脑插入新行。

召回长期记忆时标记其来源和确认状态。历史案例只能作为参考证据，不得覆盖本次实时工具观察。

将历史案例匹配实现为独立的 `app/capabilities/history.py` 能力，并只在用户询问同类故障、Planner 需要历史先例或当前症状/根因签名适合复用时触发。输入当前组件、症状、候选根因、故障路径和关键 Observation；输出案例 ID、相似度、共同点、差异点、参考处理方案、避坑提示和证据引用。默认只检索 `confirmed` 案例；与实时证据冲突时突出差异并服从实时 Observation。

## 11. 保证输出可解释

让最终报告稳定包含：

1. 故障摘要。
2. 故障传导链路。
3. 根因结论与置信度。
4. 按结论分组的证据引用。
5. 分步骤修复建议。
6. 风险、前置条件和回滚提示。
7. 证据不足项与下一步检查建议。
8. 命中的相似历史案例。

让 API 返回结构化 JSON，并让演示 UI 只负责渲染。运行事件需包含可公开的 Action、Observation 摘要和停止原因，不包含原始思维链。不得只返回一大段不可解析文本。

## 12. 按垂直切片进行 vibe coding

每次改动执行以下步骤：

1. 检查相关代码、测试、数据模型和配置。
2. 用一句话定义本次切片的用户可见结果。
3. 写出 2–5 条可验证验收条件。
4. 先补齐或更新领域模型和失败路径。
5. 实现贯穿 API、ReAct 决策、Action 执行、Observation 回写、检索/记忆和持久化的最小闭环。
6. 增加单元测试，并为跨模块行为增加一个集成测试。
7. 运行仓库已有的格式化、静态检查和测试命令。
8. 汇报已验证结果、未覆盖风险和建议的下一个最小切片。

避免一次生成大量未接通的文件、抽象层或 TODO。不得用占位实现通过测试；不得吞掉异常；不得在业务代码中硬编码演示答案。

保持异步 I/O、依赖注入、集中配置和结构化日志。将提示词版本化并与节点代码分离。只在复用已出现时提取抽象，不做预防性框架设计。

### 12.1 保证学习与求职可解释性

本项目同时是学习材料和求职作品。所有人工编写的代码、配置、测试和脚本必须包含详细说明，至少解释文件职责、核心技术原理、数据流、关键设计取舍、失败路径和验证方式。注释重点解释“为什么这样设计”和“边界如何保证”，不要逐行翻译代码或堆砌无信息注释。

新增技术、基础设施、协议边界或架构决策时，同步更新 `docs/implementation-guide.md`，列出原理、调用链、关键文件、限制和验证命令。JSON、锁文件、图片、DOCX 等不支持注释或机器生成的文件，通过相邻文档、Schema 测试和实现指南说明，不手工修改生成内容来添加注释。

将学习型说明纳入完成定义：缺少模块级 docstring、关键算法注释、技术原理文档或对应测试时，不得声称切片完成。

## 13. 建立可复现评测

维护一组小而精的 Golden Cases，至少覆盖：

- 单组件明确故障。
- 跨组件连锁故障。
- 模糊描述与缺失参数。
- 工具异常或证据冲突。
- 相似案例记忆召回。
- 无法确定根因时的安全降级。

评测意图识别、根因命中、故障链路完整性、证据引用完整率、必要 Action 覆盖率、无效/重复 Action 率、无依据结论率、工具成功率、延迟和 token 成本。

将所有数值标记为“实测值”或“目标值”。没有评测结果时，不得在 README、产品文档或简历中宣称提升百分比。

## 14. 完成门槛

在声称功能完成前确认：

- 主流程可从干净环境启动。
- Planner 真实执行有界 ReAct Action / Observation 循环，能在证据充分、需要补参或预算耗尽时正确停止。
- 9 个指定 MCP 只读工具均经过协议边界、可独立测试并留下 trace。
- GraphRAG 返回并使用真实关系路径。
- Auditor 能拦截一个故意注入的无依据结论。
- 长期记忆能完成新增、去重更新和下一会话召回。
- 历史案例匹配 capability 能按需返回相似案例、差异点、参考方案和避坑提示，且不覆盖实时证据。
- 关键失败场景有测试。
- 代码中没有真实凭据、真实生产数据或未解释的跳过项。
- 变更通过仓库已有测试与静态检查；若命令不存在，明确说明并补充最小可执行检查。

交付改动时简要列出：实现结果、关键文件、已运行验证、剩余风险和下一步建议。不要把未实现的规划描述成已完成能力。
