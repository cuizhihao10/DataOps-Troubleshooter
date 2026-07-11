# DataOps Troubleshooter Prompt 契约

本文件定义需要进入版本控制、测试和评测的核心 Prompt。产品范围与验收以 `docs/product-design.md` 为准；这里提供编码时可直接落地的输入结构、输出 Schema 和约束。

## 1. 通用原则

- 为 Prompt 设置稳定 ID 和版本，例如 `planner-react:v1`、`graphrag-entity-extract:v1`。
- 将角色说明、运行时上下文、工具 Schema 和输出 Schema 分开组织，不在代码中拼接难以审计的大段字符串。
- 使用 Pydantic 或等价 JSON Schema 校验模型输出；校验失败只允许一次受控修复，不得把自由文本直接传给工具或数据库。
- 不请求、保存或展示模型原始思维链。允许输出短 `decision_summary`、假设变化、证据缺口和停止原因。
- 所有事实结论都必须引用工具 Observation、知识节点、GraphRAG 路径或已确认案例。

## 2. Planner ReAct Prompt

### 2.1 用途

驱动 Planner 在当前状态上选择一个结构化 Action，或明确结束调查、请求用户补充信息。Observation 由确定性 MCP 工具节点生成，不由模型填写。

### 2.2 模板

```text
你是 DataOps Troubleshooter 的 Planner ReAct Agent，负责排查脱敏的
LTS、BDS、FlashSync 故障。你可以在内部分析，但不要输出逐步思维过程。

【用户问题】
{user_query}

【当前短计划】
{plan}

【当前领域能力】
{active_capabilities}

【当前假设】
{hypotheses}

【已有证据与 Observation】
{evidence_bundle}

【GraphRAG 路径】
{retrieved_paths}

【已确认历史案例】
{confirmed_case_memories}

【可用工具及参数 Schema】
{tool_schemas}

【运行预算】
当前 ReAct 步数：{react_step}
最大步骤：{max_react_steps}
剩余总时间：{remaining_time_ms}

请选择且只选择一个下一步：
1. call_tool：调用一个白名单工具；
2. finish：现有证据足以生成可审计草稿，或继续行动已无信息增益；
3. need_user_input：缺少无法通过工具获得的关键参数。

只返回符合输出 Schema 的 JSON。不要输出 Thought，不要编造 Observation，
不要重复已经成功执行过的同参工具调用。
```

### 2.3 输出 Schema

```json
{
  "status": "call_tool | finish | need_user_input",
  "decision_summary": "一到两句可公开的决策摘要",
  "hypothesis_updates": [
    {
      "hypothesis_id": "hyp_xxx",
      "status": "new | strengthened | weakened | rejected",
      "evidence_refs": ["ev_xxx"]
    }
  ],
  "action": {
    "tool_name": "lts.get_task_status",
    "arguments": {}
  },
  "evidence_refs": ["ev_xxx", "path_xxx"],
  "stop_reason": null
}
```

当 `status` 不是 `call_tool` 时，`action` 必须为 `null`；当 `status` 为 `finish` 或 `need_user_input` 时，必须提供 `stop_reason`。

### 2.4 运行时防护

- 默认最多 6 步 ReAct Action。
- 工具名必须命中白名单，参数必须通过对应 Schema 校验。
- 除可重试瞬时错误外，拒绝同一工具和参数的重复 Action。
- 工具失败后最多重试一次；仍失败时降低置信度并列出缺失证据，不得伪造实时观察。
- `decision_summary` 可进入事件时间线；内部推理文本不得进入状态、日志、API 或长期记忆。

### 2.5 运行时 capability 上下文契约

`{active_capabilities}` 使用 `runtime-capabilities:v1`。它由确定性固定 registry 根据已校验的
`intent`、组件范围和 `history_trigger` 生成，不是模型输出，也不是可动态安装的插件。
注册表恰好包含单组件诊断、跨组件链路溯源、历史案例匹配、风险评估和结构化报告五项定义；
每次选择一项主调查能力，按需追加历史能力，并始终追加风险与报告能力。

```json
{
  "contract_id": "runtime-capabilities:v1",
  "intent": "single_component_diagnosis | cross_component_diagnosis",
  "components": ["lts", "bds", "flashsync"],
  "history_trigger": "not_requested | user_requested | planner_validation | reusable_signature",
  "active_capabilities": [
    "cross_component_chain_tracing",
    "history_case_matching",
    "risk_assessment",
    "structured_reporting"
  ],
  "prompt_fragments": ["..."],
  "tool_priority": ["lts.get_task_status", "..."],
  "required_inputs": ["user_query", "components", "..."],
  "output_validation_rules": ["..."]
}
```

上游路由必须先提供强类型意图和组件范围：单组件意图恰好一个组件，跨组件意图至少两个且不能
重复。registry 不解析自然语言，也不调用 LLM、MCP、检索或记忆服务。`tool_priority` 只是
Planner 的调查建议顺序，实际 Action 仍必须通过白名单、参数、重复调用和预算校验。

历史能力只在三个批准触发条件之一出现时加入；默认 `not_requested`。无论是否启用历史能力，
实时 Observation 都高于案例和知识证据。该占位符从 v1 Prompt 起已存在，本次只是固定其首个
数据契约，因此 `planner-react:v1` 文本和输出 Schema 不升级；不兼容修改该输入语义时必须提升
capability contract，修改 Planner 行为或输出时还必须同步提升 Planner Prompt ID。

### 2.6 在线 GraphRAG 上下文契约

`{retrieved_paths}` 使用版本化的 `graphrag-retrieval:v2` 结构，新增显式 `vector_only | vector_graph | hybrid_graph` 模式；`{evidence_bundle}` 使用 `graphrag-evidence-bundle:v1`，只包含预算选中的紧凑节点和路径。这两个结构由确定性检索服务生成，不是 LLM 输出，因此 Planner Prompt 文本及输出 Schema 未变化，不提升 `planner-react:v1`；如果占位符语义或 Planner 输出发生不兼容变化，才必须提升 Planner Prompt 版本。

```json
{
  "contract_id": "graphrag-retrieval:v2",
  "query": "...",
  "mode": "hybrid_graph",
  "seed_limit": 5,
  "max_hops": 2,
  "embedding_provider": "deterministic-hash:v1",
  "score_weights": {
    "semantic": 0.45,
    "lexical": 0.10,
    "path": 0.25,
    "reliability": 0.10,
    "freshness": 0.10
  },
  "seeds": [
    {
      "node": {},
      "channels": ["lexical", "vector"],
      "semantic_score": 0.82,
      "lexical_score": 0.50,
      "reliability_score": 1.0,
      "freshness_score": 0.0,
      "hybrid_score": 0.519
    }
  ],
  "paths": [
    {
      "path_id": "path_xxx",
      "nodes": [],
      "edges": [],
      "score": 1.0,
      "hybrid_score": 0.769,
      "seed_node_id": "component_lts"
    }
  ]
}
```

`score` 在路径中专指边权乘积，`hybrid_score` 才是五项最终分。Planner 可以引用节点和 `path_id`，但不得把相似度或混合分单独当作根因证据；实时 MCP Observation 仍具有更高事实优先级。

Evidence Bundle 的上下文主体契约如下：

```json
{
  "contract_id": "graphrag-evidence-bundle:v1",
  "retrieval_contract_id": "graphrag-retrieval:v2",
  "query": "sync backlog",
  "retrieval_mode": "vector_graph",
  "budget": {"max_bytes": 6000, "max_nodes": 8, "max_paths": 4},
  "used_bytes": 4477,
  "selected_nodes": [
    {
      "evidence_id": "kn_symptom_sync_backlog",
      "node_id": "symptom_sync_backlog",
      "content": "...",
      "source_id": "synthetic_cross_chain_knowledge_v1",
      "source_span": "..."
    }
  ],
  "selected_paths": [
    {
      "evidence_id": "path_4f6638ec28f7073d",
      "path_id": "path_4f6638ec28f7073d",
      "node_ids": ["symptom_sync_backlog", "root_cause_primary_key_conflict"],
      "edge_ids": ["edge_backlog_caused_by_pk"],
      "relation_types": ["CAUSED_BY"],
      "edge_source_spans": ["同步积压由目标端主键冲突导致。"]
    }
  ],
  "omitted_node_ids": [],
  "omitted_path_ids": ["path_xxx"],
  "truncated": true
}
```

`used_bytes` 精确计算 `selected_nodes` 和 `selected_paths` 的规范 UTF-8 JSON 大小，不包含预算诊断元数据。路径只有在其全部节点、边和来源能一起进入预算时才允许注入；`truncated=true` 时 Planner 必须把 omitted IDs 视为“未注入上下文”，不能解释为知识库不存在这些候选。

## 3. GraphRAG 实体与关系抽取 Prompt

### 3.1 用途和边界

用于离线辅助整理脱敏知识种子，不位于在线诊断主链路。首版仍以人工整理和复核为准；模型输出只能形成待审核候选，不能直接写入正式图谱。

### 3.2 模板

```text
你是 DataOps Troubleshooter 的知识工程助手。请从给定的脱敏材料中，
只抽取文本明确支持的实体和关系，不补充常识，不推断材料未说明的因果。

【来源标识】
{source_id}

【允许的实体类型】
component, task, dataset, symptom, root_cause, solution, case, sop

【允许的关系类型】
RUNS_ON, DEPENDS_ON, PRODUCES, CONSUMES, MANIFESTS_AS,
CAUSED_BY, RESOLVED_BY, SIMILAR_TO

【待抽取材料】
{case_text}

要求：
1. 每个实体和关系都提供原文 source_span；
2. 使用临时 ID 连接关系，不依赖数据库正式 ID；
3. 不确定或缺少原文依据时省略，不输出猜测；
4. 只返回符合输出 Schema 的 JSON。
```

### 3.3 输出 Schema

```json
{
  "source_id": "case_seed_001",
  "entities": [
    {
      "temp_id": "e1",
      "type": "symptom",
      "name": "上游数据未就绪",
      "description": "LTS 任务等待上游数据",
      "aliases": [],
      "source_span": "上游数据未就绪",
      "confidence": 0.96
    }
  ],
  "relations": [
    {
      "from_temp_id": "e1",
      "to_temp_id": "e2",
      "type": "CAUSED_BY",
      "source_span": "上游未就绪由同步延迟导致",
      "confidence": 0.91
    }
  ]
}
```

### 3.4 入库门槛

- JSON Schema、枚举类型和临时 ID 引用全部有效。
- `source_span` 能在原始脱敏材料中精确命中。
- 实体完成规范化、别名合并和重复检测。
- 因果关系经人工或 Golden Seed 规则复核；低置信度候选不自动入库。
- 入库后保留 `source_id`、Prompt 版本和审核状态，便于追溯和重建。

## 4. 历史案例匹配 capability 契约

历史案例匹配首先使用组件/标签过滤、pgvector 相似度和 `SIMILAR_TO` 关系确定候选；模型只负责基于候选证据生成共同点、差异点、参考方案和避坑提示，不负责虚构或扩大候选集合。

```json
{
  "trigger_reason": "user_requested | planner_validation | reusable_signature",
  "matches": [
    {
      "case_id": "case_xxx",
      "similarity": 0.87,
      "confirmed": true,
      "common_points": ["..."],
      "differences": ["..."],
      "reference_actions": ["..."],
      "pitfall_warnings": ["..."],
      "evidence_refs": ["ev_xxx", "path_xxx"]
    }
  ]
}
```

只允许返回已确认案例。每个共同点、差异点和建议都必须能追溯到历史案例字段或证据；当历史案例与当前 Observation 冲突时，将冲突写入 `differences`，不得覆盖本次实时事实。
