# 第 14 章 Agent 测评：五层消融、20 个指标，以及一个仍然是 0 的数字

前十三章讲的都是"怎么让系统做对的事"。这一章讲另一件更难的事：**怎么证明它做对了，并且在它没做对的时候不允许自己糊过去**。

测评代码有一个别的模块都没有的性质：**它的结论可以由它自己伪造**。报告生成器写错了，症状是报告难看；测评脚本写错了，症状是分数变好看。而分数恰好是唯一会被抄进 README、产品文档和简历的东西。所以这一层的源码里，大部分逻辑并不在"算指标"，而在**阻止指标被算得太好看**：分母必须能从逐案明细复算、对照组必须在结构上不可能冒充实验组、任何一层失败都必须让整轮报告失去"发布资格"而不是继续输出平均值。

这一章的读法和前面一致：先看实测输出，再逐文件读源码。但最后多一个环节——讲那些**没达标的数字**，以及在"把 0 说成模型找不到根因"和"悄悄改口径让 0 变成非零"这两个都不诚实的选项之间，唯一那条诚实的路。

## 14.1 你会验证什么

评测有两个入口：**统一 CLI**（`python -m app.evaluation`，跑五层确定性评测）和**真实模型 CLI**（`python -m app.evaluation.live_golden`，会产生真实费用）。本节四条命令全部是本次实测。

快速模式，跳过需要 PostgreSQL 的两层：

```bash
.venv/Scripts/python -m app.evaluation --skip-postgres
```

输出是一份 `portfolio-eval-run:v23` JSON。抽出关键字段：

```text
contract_id=portfolio-eval-run:v23  manifest_contract_id=portfolio-eval-manifest:v24
metric_kind=measured  run_success=true  complete=false  all_suites_passed=false

graphrag_ablation         skipped      0 ms   metrics=0   PostgreSQL suite skipped by explicit fast mode.
memory_recall_ablation    skipped      0 ms   metrics=0   PostgreSQL suite skipped by explicit fast mode.
history_impact_ablation   passed    2507 ms   metrics=2
auditor_impact_ablation   passed    2409 ms   metrics=3
golden_diagnosis_baseline passed    2399 ms   metrics=11
```

20 个指标里有 16 个在快速模式下可发布，但 `complete=false` 已经声明这轮不完整。注意 `skipped` 的两层 `metrics=0`：**没跑就没有数字**，不会拿 manifest 里的历史快照来充数。

完整模式但本机没有配测试库：

```bash
.venv/Scripts/python -m app.evaluation   # 退出码 1
```

```text
run_success=false  complete=false  all_suites_passed=false
graphrag_ablation         blocked   0 ms  metrics=0  DATAOPS_TEST_DATABASE_URL is required for a complete portfolio run.
memory_recall_ablation    blocked   0 ms  metrics=0  DATAOPS_TEST_DATABASE_URL is required for a complete portfolio run.
history_impact_ablation   passed 2789 ms  metrics=2
auditor_impact_ablation   passed 2552 ms  metrics=3
golden_diagnosis_baseline passed 2399 ms  metrics=11
```

同样是"两层没跑"，状态却从 `skipped` 变成 `blocked`，退出码从 0 变成 1。区别只在于**用户有没有主动要求完整运行**——§14.9.3 会讲为什么这两种情况必须用不同的状态词。

真实模型评测的配置预检（本机未配 `DATAOPS_DATABASE_URL`）：

```bash
.venv/Scripts/python -m app.evaluation.live_golden --code-revision test123   # 退出码 2
```

```text
usage: live_golden.py [-h] --code-revision CODE_REVISION [--case-id CASE_ID]
                      [--all-cases] [--output OUTPUT]
live_golden.py: error: live Golden evaluation requires DATAOPS_DATABASE_URL
```

关键在于**这个错误发生在任何付费调用之前**，而且用的是 argparse 的短消息而不是堆栈（§14.10.1）。

本章两个直接相关的测试文件：

```bash
.venv/Scripts/python -m pytest -q tests/unit/test_entrypoint_model_rebuild.py \
                                  tests/integration/test_golden_diagnosis_evaluation.py
```

```text
15 passed in 0.83s
```

## 14.2 为什么是五层，而不是一个总分

先看全景。五层评测各自控制一个变量，源码分别在五个文件里：

| 层 | 对照组 | 实验组 | 唯一变量 | 源码 | 需要 PG |
|---|---|---|---|---|---|
| GraphRAG 消融 | `vector_only` | `vector_graph` | 图扩展开关 | `app/retrieval/ablation.py` | 是 |
| 记忆召回消融 | `vector_only` | `vector_graph` | `SIMILAR_TO` 关系扩展 | `app/memory/evaluation.py` | 是 |
| 历史影响消融 | `memory_off` | `memory_on` | 顶层工作流是否召回历史 | `app/orchestration/history_evaluation.py` | 否 |
| Auditor 影响消融 | `auditor_off` | `auditor_on` | 是否调用第二个 Agent | `app/orchestration/auditor_evaluation.py` | 否 |
| Golden 诊断基线 | 产品目标值 | 本次实测值 | 无（不是消融，是回归基线） | `app/evaluation/golden_diagnosis.py` | 否 |

**为什么不合成一个总准确率？** 因为这五层回答的是五个不同的问题，量纲和分母都不同：图扩展补齐了几条因果链、关系扩展救回了几条历史案例、历史上下文改变了几个 Action、语义 Auditor 在确定性规则放行之后还抓到几个问题、28 条 Golden 案例里有多少条命中标注。把它们平均成一个数字，唯一效果是让读者无法追问任何一项。所以 manifest 里每层都带 `layer` 描述和 `result_document` 指针，指标 ID 全局唯一但**从不跨层聚合**。

前四层是**消融**（ablation）：同一份输入跑两遍，只改一个开关，报告 `delta = treatment - control`。第五层是**回归基线**：没有对照组，只有"标注答案"和"本次结果"。这个区别在 manifest 里以 `control_label` 的取值直接体现——前四层是 `vector_only` / `memory_off` / `auditor_off` 这类模式名，第五层是 `product_target_minimum` / `safety_expectation` 这类目标名。读到 `control_value=0.8 → treatment_value=1.0` 时必须知道：**0.8 是产品文档里写的最低目标，不是"关掉某个功能测出来的 0.8"**。

五层共享三条设计约束，后面每一节都会看到它的具体形态：

1. **对照组必须在结构上不可能冒充实验组。** 不是靠"记得别写错"，而是靠类型和校验器。`vector-only` 的结果里出现 graph 通道直接抛错（§14.4），`auditor_off` 的结果只能是 `control_unreviewed` 这个专属枚举值，永远无法序列化成 `accepted`（§14.6）。
2. **分母必须能从逐案明细复算。** 报告级校验器会重新数一遍案例、类别、锚点适用数，与字段里写的值比对（§14.7.5）。手工把分母填大一点这条路是封死的。
3. **失败必须让整轮失去发布资格，而不是被记成 0 分。** 五层的 runner 协议全部要求异常向上传播。把 Provider 超时记成"模型答错了"会污染指标本身要衡量的对象。

## 14.3 第一层：GraphRAG 消融——大半代码在证明"实验条件一致"

`app/retrieval/ablation.py` 只有 179 行，`evaluate_graph_ablation` 的前 13 行全是前置断言：

```python
if vector_only.mode is not RetrievalMode.VECTOR_ONLY:
    raise ValueError("vector_only result must use vector_only mode")
if vector_graph.mode is not RetrievalMode.VECTOR_GRAPH:
    raise ValueError("vector_graph result must use vector_graph mode")
if vector_only.query != case.query or vector_graph.query != case.query:
    raise ValueError("ablation results must use the case query")
if vector_only.embedding_provider != vector_graph.embedding_provider:
    raise ValueError("ablation results must use the same embedding provider")
if vector_only.score_weights != vector_graph.score_weights:
    raise ValueError("ablation results must use the same scoring weights")
for result in (vector_only, vector_graph):
    if result.seed_limit != case.seed_limit or result.max_hops != case.max_hops:
        raise ValueError("ablation results must use the case retrieval budgets")
```

六项：模式、查询文本、embedding Provider、混合打分权重、种子上限、跳数预算。紧跟着的注释说明了为什么这六项一个都不能少：

```python
# 只有所有实验条件一致后才计算差值，避免“换 Provider/预算”被错误归因于图结构。
```

这是消融实验最常见的作弊形态，而且往往不是故意的：调完 `seed_limit` 顺手跑一遍对比，看到 delta 变大就写进文档。**代码不允许这种数字存在**——条件不一致时得到的不是一个偏高的 delta，而是一个异常。

### 14.3.1 两个指标：可见性与有序链路

`_mode_metrics` 算两件事。第一件是根因是否可见：

```python
visible_node_ids = {seed.node.node_id for seed in result.seeds}
visible_node_ids.update(node.node_id for path in result.paths for node in path.nodes)
expected_root_causes = set(case.expected_root_cause_node_ids)
root_cause_hit = bool(visible_node_ids & expected_root_causes)
```

注意可见集合是**种子 ∪ 路径节点**的并集。这是刻意的：图扩展的价值之一就是把向量检索没排进 top-k 的节点通过关系带进来，所以并集才是"这一轮 Planner 有机会看到的知识节点"。只数种子会低估图，只数路径节点会漏掉本来就排上来的根因。

第二件是必要因果链的覆盖率。`_ordered_path_coverage` 是一个双指针：

```python
matched = 0
for node_id in actual:
    if matched < len(required) and node_id == required[matched]:
        matched += 1
return matched / len(required)
```

它计算的是**最长有序子序列覆盖率**：实际路径可以包含额外的中间节点（`A → X → B` 命中 `A → B`），但不允许倒序命中（`B → A` 不算命中 `A → B`）。因果链一旦倒序，方向就反了——"BDS 分区缺失导致 LTS 任务阻塞"和"LTS 任务阻塞导致 BDS 分区缺失"是两个完全不同的诊断。函数 docstring 也交代了它的适用边界：**"该指标适合一至两跳小图，不引入复杂图编辑距离"**，而 `GraphAblationCase.max_hops` 的取值范围恰好是 `ge=1, le=2`。指标和数据的约束是配套的。

### 14.3.2 实测值：一个 Δ0 和一个 Δ1.0

| 指标 | vector_only | vector_graph | delta | 说明（manifest 原文） |
|---|---|---|---|---|
| `graph_root_cause_hit` | 1.0 | 1.0 | 0.0 | 根因在向量基线已可见，图扩展没有虚报额外根因收益。 |
| `graph_chain_completeness` | 0.0 | 1.0 | 1.0 | 真实 PostgreSQL 图路径补齐了预先标注的必要因果链。 |

第一行的 Δ0 是这一层最值得讲的数字：**图扩展在这个案例上对"能不能看到根因节点"毫无贡献**，因为向量检索本来就把它排上来了。很多项目会把这一行删掉只留第二行，那样读者会自然以为"引入图让根因命中提升了"。留着 Δ0 才说明这层实验真的在测量。

第二行的 Δ1.0 也需要正确解读：`vector_only` 的 `chain_completeness` **必然是 0**，因为 vector-only 模式根本不产生 `paths`，而链路覆盖率只从路径的有序 `node_ids` 计算（docstring 里写明"vector-only 因没有路径自然为零"）。所以这个 Δ1.0 证明的是"图给出了完整的、方向正确的因果链"，**不是**"图让答案更准了"。这两句话的差别，就是这一层能宣称和不能宣称的边界。

## 14.4 第二层：记忆召回消融——corpus 里不许有 pending

`app/memory/evaluation.py`（371 行）测的是长期记忆检索：`pgvector` 向量召回 vs 向量 + `SIMILAR_TO` 图关系扩展。它的第一个判断在数据层：

```python
if self.status is MemoryStatus.PENDING:
    raise ValueError("memory recall eval corpus status must be confirmed or rejected")
```

corpus 只允许 `confirmed` 和 `rejected`。docstring 解释了为什么：**"这样评测不会把'尚未审核'与'已审核后撤销'混成一个禁止标签"**。第 10 章讲过 `case-memory:v2` 的三态：`pending` 是候选、`confirmed` 可召回、`rejected` 是明确撤销。如果 corpus 里混入 `pending`，那么"没召回它"既可能是"隔离机制正确"也可能是"检索漏了"，禁止命中数就不再是一个安全指标。于是这一层只比较**默认可见**和**明确撤销**两种确定语义。

### 14.4.1 三个标签集合的约束

每条 case 有三个标签集合，校验器逐一约束它们：

```python
if set(self.expected_labels) & set(self.forbidden_labels):
    raise ValueError("expected and forbidden memory labels must not overlap")
if not set(self.expected_graph_only_labels) <= set(self.expected_labels):
    raise ValueError("graph-only memory labels must be expected labels")
```

`expected_graph_only_labels` 必须是 `expected_labels` 的**子集**，这是这一层的核心断言：它标注的是"这条案例只能靠图关系救回来"。如果允许它包含非期望标签，那么"图额外召回了一堆无关案例"也会被算成图的贡献。

单条 case 无法确认标签是否真的存在于 corpus，所以跨对象检查放在 suite 层：label / root_cause / case_id / query 四项各自唯一，然后逐条检查引用：

```python
unknown = sorted(referenced - known_labels)
if unknown:
    raise ValueError(
        f"memory recall eval case {case.case_id} references unknown labels: {unknown}"
    )
```

这个顺序（先查重复定义，再查悬空引用）是刻意的，docstring 说明了理由：**"失败消息因此能区分重复定义和悬空引用"**。一个写错的标签名和一个复制粘贴出来的重复标签，修法完全不同。

### 14.4.2 一行代码证明消融真的关掉了图

`_mode_metrics` 的第一件事不是算指标，是验证对照组的纯度：

```python
if mode is MemoryRetrievalMode.VECTOR_ONLY and any(
    MemoryRetrievalChannel.GRAPH in match.retrieval_channels for match in matches
):
    raise ValueError("vector-only memory evaluation cannot contain graph matches")
```

`case-memory:v2` 要求每条命中记录自己是从哪个通道来的（`VECTOR` / `GRAPH`）。于是"消融是否真的生效"这个问题有了**结构性答案**：vector-only 那一遍的返回值里如果出现任何 graph 通道，评测直接崩。这比"我在调用里传了 `mode=VECTOR_ONLY`，所以它应该关了"强得多——后者是意图，前者是证据。

对应地，图救回的判定要求通道组合精确：

```python
graph_only_hits = [
    label
    for label, match in zip(labels, matches, strict=True)
    if label in case.expected_graph_only_labels
    and MemoryRetrievalChannel.GRAPH in match.retrieval_channels
    and MemoryRetrievalChannel.VECTOR not in match.retrieval_channels
]
```

**有 graph 且没有 vector**。一条同时被两个通道命中的案例不算"图救回"，因为向量本来就能找到它——给它附加一个 graph 标记不产生任何检索价值。`zip(..., strict=True)` 保证标签列表和命中列表长度一致，长度不一致是投影逻辑写错了，不该静默截断。

### 14.4.3 未知根因不能丢

```python
# 未知 root cause 不能被过滤，否则 precision 会被人为抬高；保留 memory_id 还能回查污染来源。
labels = [
    root_to_label.get(match.memory.root_cause, f"unknown:{match.memory.memory_id}")
    for match in matches
]
```

检索返回了一条不在 corpus 里的案例，说明数据库被别的测试污染了，或者相似度阈值太松。这时候有两种写法：过滤掉它（`precision = 命中/剩下的`），或者留着当 false positive（`precision = 命中/全部返回`）。前者会让 precision 无条件升高，而且升高的幅度正比于污染程度——**污染越严重，分数越好看**。所以这里保留它，并且带上 `memory_id` 以便回查。

`recall_at_k` 和 `precision_at_k` 的分母也因此是不同的：前者是标注的 expected 数量，后者是实际返回数量（`len(labels)`，空结果时 precision 定为 0.0 而不是 1.0）。

### 14.4.4 增益与回归对称暴露

```python
graph_rescued_labels=[
    label for label in case.expected_labels
    if label not in vector_hits and label in graph_hits
],
regressed_labels=[
    label for label in case.expected_labels
    if label in vector_hits and label not in graph_hits
],
```

两个列表结构完全对称：图救回的和图丢掉的。这一点在消融实验里很容易只做一半——因为只有第一个列表能写进简历。`MemoryRecallCaseReport` 的 docstring 明确要求：**"`regressed_labels` 表示反向丢失，评测测试必须显式审阅而不能隐藏"**。

宏观汇总用 macro 平均（每条查询等权，不按 expected 数量加权），`forbidden_hit_count` 单独列出来当安全门禁——它是一个计数而不是比率，因为"撤销的案例被召回了 1 次"和"被召回了 3 次"都是同一件事：隔离失效。

### 14.4.5 实测值

| 指标 | vector_only | vector_graph | delta |
|---|---|---|---|
| `memory_macro_recall_at_k` | 0.9167 | 1.0 | +0.0833 |
| `memory_macro_precision_at_k` | 0.9167 | 1.0 | +0.0833 |

0.9167 = 11/12。六条确定性角度查询里，五条 recall 已经是 1.0，第六条是 0.5，图关系把这一条补齐成 1.0，于是 macro 从 5.5/6 升到 6/6。manifest 的 note 保留了两条限制：**"六条确定性角度查询的小样本实测，不能外推到通用语义模型"**（角度向量是测试 Provider 生成的单位向量，不是 `bge-m3` 的真实语义空间），以及 **"rejected 案例禁止命中保持为零，增益只来自固定图救回案例"**。

## 14.5 第三层：历史影响消融——第一次跑完整端到端

前两层都只测检索。`app/orchestration/history_evaluation.py`（494 行，`history-impact-eval:v1`）测的是**整条 `AuditedDiagnosisWorkflow`**：同一条案例跑两遍完整工作流（召回 → ReAct → 解释 → 报告 → 审计 → 暂存），唯一变量是顶层是否召回历史案例。

模块 docstring 先划清了它不做什么：**"评测不读取模型 Thought，也不把历史相似度当作当前事实置信度。"** 前半句是第 12 章的推理过程不外泄边界在评测侧的延续，后半句是第 10 章的实时优先原则——历史案例的相似度高不代表这次的结论就对。

### 14.5.1 suite 必须包含一条冲突案例

`HistoryImpactEvalSuite` 的 docstring 写了一条容易被跳过的要求：

> 首版至少要求三条案例，匹配产品设计中的三类长期记忆 Golden Case；同时至少包含一条历史冲突案例，防止评测只证明"案例能展示"却没有验证"旧结论不能覆盖实时 Observation"。

```python
if not any(case.expect_history_conflict for case in self.cases):
    raise ValueError("history impact eval suite requires a conflict guard case")
```

这是 schema 级别的**覆盖率约束**，不是"记得加一条"。理由很直接：历史召回这个功能的收益（多看到几条相似案例）容易测，风险（旧结论污染新诊断）容易漏。只测收益的评测会给出一个漂亮的正 delta，同时对最危险的失败模式一无所知。所以 suite 拒绝加载。

配对的还有 case 级别的约束：`expect_history_conflict=True` 的案例必须提供至少一个 `forbidden_root_causes`，否则"实时优先"没有可执行的判定标准：

```python
if self.expect_history_conflict and not self.forbidden_root_causes:
    raise ValueError("history conflict cases require at least one forbidden root cause")
```

还有一条跨组件越界检查，注释解释了它防的是什么：

```python
# 工具名称的协议前缀就是组件标识；在加载 fixture 时拦截跨组件工具，避免评测脚本自己
# 违反 capability 白名单后仍把失败误报为 Planner 退化。
component_values = {component.value for component in self.components}
```

工具名形如 `lts.get_task_status`，前缀就是组件。如果案例只声明了 `bds` 组件却把 `lts.*` 列进必要 Action，那么 Planner 无论多聪明都覆盖不了——它会被 capability 门禁整批拒绝（第 6 章）。这时得到的 0.6667 覆盖率不是模型的问题，是标注的问题。在**加载阶段**拦截，比在报告里看到一个低分再回头查两小时便宜得多。

### 14.5.2 配对校验：不许偷换输入

`_validate_paired_results` 有五条断言，前四条约束两个模式的历史语义，最后两条约束输入：

```python
if memory_off.history_trigger is not HistoryTrigger.NOT_REQUESTED:
    raise ValueError("memory-off history impact result must use not_requested trigger")
if memory_off.recalled_memories or memory_off.history_case_matches:
    raise ValueError("memory-off history impact result cannot contain recalled history")
if memory_on.history_trigger is HistoryTrigger.NOT_REQUESTED:
    raise ValueError("memory-on history impact result must trigger history recall")
if len(memory_on.recalled_memories) < case.minimum_history_matches:
    raise ValueError(
        f"memory-on case {case.case_id} returned fewer than the required history matches"
    )
if memory_off.report.state.user_query != case.user_query:
    raise ValueError("memory-off result changed the eval user query")
if memory_on.report.state.user_query != case.user_query:
    raise ValueError("memory-on result changed the eval user query")
```

对照组的纯度这次由两个字段共同保证：`history_trigger` 必须是 `NOT_REQUESTED`（意图），并且 `recalled_memories` 与 `history_case_matches` 都必须为空（结果）。只查前者会漏掉"trigger 写对了但召回代码仍然跑了"的情况。

实验组反过来：必须真的触发，而且召回数不能低于案例标注的 `minimum_history_matches`。**召回 0 条的 memory_on 不是"历史没帮上忙"，是这次实验没做成**——它不该贡献一个 delta=0 的数据点，而该让整轮失败。

最后两条查的是 `report.state.user_query` 而不是入参：docstring 写明目的是"防止 runner 偷换输入制造虚假增益"。这是消融实验里最隐蔽的一种作弊——给实验组一个信息更全的提问。检查落在**最终状态**上，因为那是真正流经工作流的那份文本。

### 14.5.3 Action 覆盖率的三个口径细节

```python
# ToolEvent 代表工具已实际进入执行边界；Planner 仅提出但被策略门禁拦截的 Action 不计覆盖。
executed_tools = _stable_unique_tools(
    [event.tool_name for event in result.react.state.tool_events]
)
```

三件事同时被这两行钉住：

1. **只数 `ToolEvent`，不数 Planner 的 Action 提案。** Planner 说要查 LTS 日志但被并行上限或指纹去重拒绝了（第 6 章的整批拒绝），这次调用就没有发生，不能算覆盖。
2. **按首次出现去重**（`dict.fromkeys`）。一个工具重试了一次会产生两个 `ToolEvent`，但它只解决了一个必要动作。不去重的话"网络抖动多重试几次"会让覆盖率虚高。
3. **粒度是工具名，不是参数组合。** `_stable_unique_tools` 的 docstring 交代了边界："同一工具不同参数仍只计一次名称级 Golden Action 命中，因为当前 fixture 的必要动作标注粒度就是产品设计定义的九个工具名。"

`unexpected_action_rate` 的分母是 `len(executed_tools)`（去重后实际执行的工具数），空列表时定为 0.0。而实时引用率只认 `EvidenceSourceType.TOOL`：

```python
realtime_refs = {
    evidence.evidence_id
    for evidence in result.react.state.evidence
    if evidence.source_type is EvidenceSourceType.TOOL
}
```

历史案例 ID 和知识图节点都不算实时支撑。一个只引用了历史案例的根因，在这个指标下是 0 分——这正是"实时 Observation 永远优先于历史案例"这条产品约束的可测形态。

### 14.5.4 冲突保护：`None` 不进分母

`_evaluate_conflict_guard` 的返回类型是 `bool | None`，三个取值有三种含义：

```python
if not case.expect_history_conflict:
    return None
```

`None` = 这条案例不适用，**不进入 conflict pass rate 的分母**。这是分母诚实性的一个具体实现：七条不涉及冲突的案例不会因为"没有违规"而白送七个满分，也不会因为"没有冲突可保护"而被记成失败。汇总时的过滤条件对应地写成 `if report.memory_on.conflict_guard_passed is not None`。

`False` 有两条路径。一是**实验没做成**：标注说这条案例应该召回一条冲突历史，但 `recalled_memories` 里没有任何一条的根因落在 `forbidden_root_causes` 里，说明 fixture 或检索出了问题。二是**保护真的失效**：召回了冲突案例，但最终报告没有正确标注它。

`True` 的条件相当严格：

```python
has_root_difference = any(
    "根因" in item and ("不一致" in item or "冲突" in item)
    for item in reference.differences
)
blocks_direct_reuse = any("禁止直接复用" in item for item in reference.pitfall_warnings)
if not has_root_difference or not blocks_direct_reuse:
    return False
```

每一条冲突历史都必须在最终 `SimilarCaseReference` 里同时满足两件事：`differences` 里有一条同时提到"根因"和"不一致/冲突"，`pitfall_warnings` 里有一条包含"禁止直接复用"。

用中文子串做断言看着很土，值得说清它的适用边界。这里能这么写，是因为这两句话由 `explain_case_matches` 节点**确定性生成**（第 10 章），不是模型自由发挥的文本——它检查的是确定性解释器有没有把冲突标出来，而不是在评价模型的措辞。如果哪天这段文本改由模型生成，这个判据必须换成结构化字段，否则就变成了在测提示词的用词习惯。

顺便看一眼 `_average`：

```python
if not values:
    raise ValueError("history impact metric average requires at least one value")
```

suite schema 已经保证 cases 非空，这条断言仍然留着，docstring 说明理由："避免未来独立调用时用除零或默认零掩盖缺失结果"。**空平均值返回 0.0 是本章反复出现的错误模式**——它把"没测"伪装成"测出来是 0"。

### 14.5.5 实测值

| 指标 | memory_off | memory_on | delta | 方向 |
|---|---|---|---|---|
| `history_necessary_action_coverage` | 0.6667 | 1.0 | +0.3333 | 越高越好 |
| `history_unexpected_action_rate` | 0.3333 | 0.0 | −0.3333 | 越低越好 |

三条案例，macro 平均。0.6667 = 2/3：关掉历史后，有一条案例的必要 Action 没被完整覆盖。第二行是同一件事的另一面——它多做了不该做的调用。

manifest 的两条 note 限制了它能宣称的范围：**"确定性 Planner 三案例实测，只证明历史上下文进入真实 LangGraph。"** 以及 **"根因命中和 TOOL 引用两组均为 1.0，未宣称额外准确率提升。"**

第二条尤其重要。这一层跑出来的**根因命中率两组都是 1.0**，实时引用率两组也都是 1.0。也就是说：历史上下文改变了 Planner 的动作选择，但**没有改变最终答案的正确性**。写进文档的时候，只能说"历史上下文进入了真实工作流并改变了行为"，不能说"历史召回提升了诊断准确率"。这两个 Δ0 和第一层的 `graph_root_cause_hit` Δ0 一样，是这份报告可信度的来源，而不是缺陷。

## 14.6 第四层：Auditor 影响消融——`auditor_off` 不是生产开关

`app/orchestration/auditor_evaluation.py`（474 行，`auditor-impact-eval:v1`）测的是第 9 章那个独立 Auditor 的**增量**贡献：在确定性规则（`app/reporting/policy.py`）已经放行之后，第二个 Agent 还能抓到什么。

这一层最容易被误读，所以源码里有三处专门防误读的设计。第一处在枚举的 docstring 里：

> `auditor_off` 不表示生产可关闭审计，只表示评测 runner 在同一确定性 Validator 后不调用第二个 Agent；`auditor_on` 必须运行完整报告工作流。

模块 docstring 也重复了一遍："产品运行时始终要求 Auditor。"这不是客套——一份写着"关掉审计后危险内容残留率 1.0"的报告，很容易被读成"这个系统有一个可以关掉审计的配置项"。它没有。

第二处是 `AuditorImpactOutcome` 的三值设计：

```python
CONTROL_UNREVIEWED = "control_unreviewed"
ACCEPTED = "accepted"
DEGRADED = "degraded"
```

docstring 说明了为什么要多出第一个值：**"off 组只能是 `control_unreviewed`，不能伪装成 Auditor accept。"** 如果沿用生产的 `accepted`/`degraded` 二值枚举，off 组的报告未经审查却会被序列化成 `accepted`——一个下游读者（或者半年后的作者本人）看到 `outcome=accepted` 完全无法分辨这份报告是通过了审计还是根本没审。**"未审计"和"审计通过"必须是两个不同的词**，而且要在类型层面不可互换。

第三处是 `validate_mode_semantics` 的五条 off 组约束：

```python
if self.mode is AuditorImpactMode.AUDITOR_OFF:
    if self.auditor_called:
        raise ValueError("auditor-off control cannot call the Auditor")
    if self.outcome is not AuditorImpactOutcome.CONTROL_UNREVIEWED:
        raise ValueError("auditor-off control must be marked unreviewed")
    if self.audit_issue_codes or self.revision_count != 0:
        raise ValueError("auditor-off control cannot contain audit results or revisions")
    if self.final_report != self.draft_report:
        raise ValueError("auditor-off control must preserve the original draft")
    return self
```

最后一条是关键：off 组的 `final_report` 必须**逐字段等于** `draft_report`。`DiagnosisReport` 是 Pydantic 模型，`!=` 是深比较。这意味着 off 组不可能悄悄"顺手清理了一下报告"——任何修改都会让评测崩掉。对照组的定义因此是可执行的，而不是文档里的一句承诺。

### 14.6.1 "增量"的定义：规则预检必须是空的

`_validate_paired_runs` 里有一条断言，把这一层的语义收得非常窄：

```python
if auditor_off.deterministic_issues != auditor_on.deterministic_issues:
    raise ValueError("auditor impact paired runs must use the same deterministic precheck")
if auditor_off.deterministic_issues:
    raise ValueError("auditor impact incremental cases must pass deterministic precheck")
```

第一条要求两组的确定性预检结果相同（否则变量就不止一个）。第二条更强：**确定性预检必须为空**。docstring 给了理由：

> 只有确定性问题为空时，on 组新增问题才可归因给语义 Auditor；若规则已经发现缺陷，评测应改用规则门禁测试而非本消融。

这是"增量贡献"这个词的精确含义。如果一条案例的缺陷连正则规则都能抓到（比如引用 ID 不存在），那么它证明的是规则有效，不是语义 Auditor 有价值。把这种案例混进来会让 `auditor_expected_issue_detection` 的 1.0 变得没有意义——读者以为是"模型发现了规则发现不了的问题"，实际上只是"规则发现的问题在 on 组也被报了一遍"。

所以这一层的三条案例（对应 `AuditorDefectType` 的三个值，suite 校验器要求**恰好覆盖全部**）都是**通过了所有客观检查、但语义上仍然有问题**的报告：

| 缺陷类型 | 含义 |
|---|---|
| `unsupported_root_cause` | 引用 ID 都存在，但引用的内容不支持这个根因 |
| `evidence_conflict` | 实时证据之间有冲突，但没有写进结构化 `contradicting` 字段 |
| `unsafe_remediation` | 字段完整、引用齐全，但建议的动作本身不安全 |

这三类恰好是正则和 Schema 检查不到的东西——它们需要读懂内容。这也回答了第 9 章那个问题："既然有确定性规则，为什么还要花钱调第二个模型？"答案在这张表里：三类缺陷，规则一个也抓不到。

### 14.6.2 多报问题不加分

```python
# 额外多报问题可以保留供人工复核，但只有 fixture 预期 code 能贡献发现率，防止刷高指标。
expected = set(case.expected_issue_codes)
expected_hits = [code for code in case.expected_issue_codes if code in audit_codes]
missing_expected = [code for code in case.expected_issue_codes if code not in audit_codes]
```

发现率的分子只数 fixture 预期的 code，分母是预期 code 的数量。Auditor 额外报的问题保留在 `audit_issue_codes` 里供人工看，但不进分子也不进分母。

这条规则挡住的是一个很实际的失败模式：让 Auditor 变得非常多疑，什么报告都报一堆问题。这样的模型在"发现率"上会拿满分，代价是把大量正确报告打回返工。发现率本身**衡量不了误报**，所以设计上就不让多报的问题产生任何收益，误报的代价则由第 9 章的返工预算（`max_audit_revisions ≤ 1`）和 `auditor_safe_resolution_rate` 承担。

安全指标是双向的：

```python
unsafe_marker_count = len(case.unsafe_root_causes) + len(case.unsafe_action_fragments)
unsafe_retained_count = len(unsafe_roots) + len(unsafe_actions)
...
unsafe_item_rate=unsafe_retained_count / unsafe_marker_count,
safe_resolution=unsafe_retained_count == 0,
```

`unsafe_item_rate` 是残留比例（越低越好），`safe_resolution` 是**全清才算通过**的布尔量。两个都保留，因为"三条危险建议删掉了两条"在比例上是 0.333 的进步，在安全上仍然是失败。

### 14.6.3 实测值：唯一一组 0 → 1

| 指标 | auditor_off | auditor_on | delta | 方向 |
|---|---|---|---|---|
| `auditor_expected_issue_detection` | 0.0 | 1.0 | +1.0 | 越高越好 |
| `auditor_unsafe_item_rate` | 1.0 | 0.0 | −1.0 | 越低越好 |
| `auditor_safe_resolution_rate` | 0.0 | 1.0 | +1.0 | 越高越好 |

这是五层里唯一一组"从 0 到 1"的 delta，也是最容易被过度解读的一组。off 组的三个数字全是极值，原因很简单：**不调用 Auditor 就必然发现 0 个问题、必然保留全部危险内容**。这三行不是在证明"Auditor 让系统好了三倍"，而是在证明"这三类缺陷只有语义审计能处理"。

manifest 的三条 note 把边界写清了：

- **"off 是 control_unreviewed 评测对照，不是生产开关或 accepted。"**
- **"三条确定性语义缺陷脚本只验证修订与降级控制流。"**——缺陷是脚本化注入的，不是真实模型自然产生的分布。
- **"两例修订后接受，一例持续冲突后安全降级；降级不等于接受。"**

第三条对应第 9 章的四级阶梯：三条案例里有一条在返工后仍然存在冲突，返工预算耗尽转 `degraded`。它在 `auditor_safe_resolution_rate` 里算通过（危险内容清零了），但它的 `outcome` 是 `degraded` 而不是 `accepted`——报告带着"未能完全消解冲突"的声明交付给用户。**安全处置 ≠ 审计通过**，这一层的报告结构把两者分开记录。

## 14.7 第五层：Golden 诊断基线——823 行里最长的一段

`app/evaluation/golden_diagnosis.py`（823 行，`golden-diagnosis-eval:v23`）不是消融，是**回归基线**：28 条标注案例，把本次实测值和产品文档里的目标值并列。它是五层里最长的一个文件，因为它要对一份完整报告的十一个维度分别打分，而每个维度都有自己的分母和适用性判定。

### 14.7.1 一个 Protocol，两个 runner

文件里最重要的一个设计决策只有几行：

```python
class GoldenDiagnosisRunner(Protocol):
    async def run(self, case: GoldenDiagnosisCase) -> GoldenDiagnosisObservation: ...
```

打分器只依赖这个 Protocol，于是**同一套评分逻辑同时服务确定性基线和真实模型评测**：确定性侧是 `tests/integration/` 里的 `FixtureBackedGoldenRunner`（§14.8），真实模型侧是 `app/evaluation/live_golden.py` 的 `LiveGoldenRunner`（§14.10）。

这不是为了代码复用，是为了**口径可比**。如果确定性基线和真实模型各自实现一套打分，那么"确定性 1.0，真实模型 0.667"这句话就没有意义——两个数字可能连分母定义都不同。共用打分器之后，两条路径的差异被压缩到唯一一个地方：谁来生成那份 `GoldenDiagnosisObservation`。

Protocol 的 docstring 还钉了一条错误处理约定：**"异常应向上传播，不能伪装成零分案例。"** 这是 §14.2 第三条约束在这一层的落点。Provider 超时、数据库断连、MCP 子进程起不来——这些都不是"模型答错了"。把它们记成 0 分会让指标衡量的对象从"模型能力"变成"模型能力 × 基础设施可用性"，而后者根本不是这份报告声称要测的东西。

### 14.7.2 28 条案例的配额是写死的

```python
GOLDEN_DIAGNOSIS_TARGET_CASE_COUNT = 28
GOLDEN_DIAGNOSIS_CATEGORY_TARGETS: dict[GoldenCaseCategory, int] = {
    GoldenCaseCategory.SINGLE_COMPONENT: 8,
    GoldenCaseCategory.CROSS_COMPONENT: 10,
    GoldenCaseCategory.AMBIGUOUS_OR_INSUFFICIENT: 4,
    GoldenCaseCategory.TOOL_ANOMALY_OR_CONFLICT: 3,
    GoldenCaseCategory.MEMORY_RECALL: 3,
}
```

这五个数字来自 `docs/product-design.md`，不是从现有案例数反推的。差别在于：写死之后，"当前跑了多少条"和"目标是多少条"变成两个独立字段，`case_coverage_rate = case_count / target_case_count` 于是可以小于 1，而且这个小于 1 会被 `target_coverage_complete=false` 明确标出来。

`validate_case_coverage` 把这套账全部重算一遍：

```python
if self.case_count != len(self.cases):
    raise ValueError("golden diagnosis case_count must match case details")
...
if self.case_count > self.target_case_count:
    raise ValueError("golden diagnosis case_count cannot exceed the versioned target")
expected_rate = self.case_count / self.target_case_count
if abs(self.case_coverage_rate - expected_rate) > 1e-6:
    raise ValueError("golden diagnosis case coverage rate is inconsistent")
```

注意第二条：**案例数超过目标也拒绝**。这条第一次读会觉得奇怪——多测几条不是更好吗？docstring 给了理由："因为这意味着产品目标或契约版本应先显式升级。"如果允许悄悄加案例，那么"覆盖率 1.0"就不再对应"产品设计里定义的那 28 条"，而是对应"作者最近顺手加的那些"。要扩集合，就得升 `golden-diagnosis-eval` 的版本号并同步产品文档（还有 §14.9.1 那张钉住表）。

类别配额同样双向校验：实际计数必须能从明细复算（`actual_category_counts` 逐条累加后比对），目标必须逐字等于产品设计的常量，目标之和必须等于总目标，且**每一类的实际数不能超过它自己的配额**。最后这条防的是"用 10 条容易的单组件案例凑够 28 条"——那样总数达标了，但跨组件、歧义、工具异常三类的覆盖是假的。

### 14.7.3 分区校验：标签不许凭空消失

`validate_fault_path_partition` 检查的是每条案例内部的一致性。核心是"精确分区"：

```python
if set(matched) & set(missing) or set(matched) | set(missing) != set(required):
    raise ValueError("Golden case result path labels must form an exact partition")
if bool(required) != (self.fault_path_completeness is not None):
    raise ValueError("Golden case result path applicability is inconsistent")
```

第一条同时挡住两种错误：同一条路径既算命中又算缺失（交集非空），以及某条标注的路径从明细里凭空消失（并集不等于 required）。后者是分母造假最省事的做法——把没命中的那条从 `missing` 里删掉，命中率立刻变成 1.0，而且逐案明细看起来完全自洽。**要求并集精确等于标注集**之后，这条路被封死。

第二条是**适用性**约束：没有标注路径的案例，`fault_path_completeness` 必须是 `None`，不能是 0.0 也不能是 1.0。这是本章最重要的一个口径概念——`None` 表示"不进分母"，`0.0` 表示"进了分母但没做到"。混淆这两者会让指标同时被两个方向污染：不适用的案例记 1.0 会白送满分，记 0.0 会白背失败。

同样的适用性检查覆盖三组可选字段：路径、历史（只有 `MEMORY_RECALL` 类别可以有）、冲突（只有 `TOOL_ANOMALY_OR_CONFLICT` 类别可以有）。而且是**双向**的——非 memory 案例带了历史字段也报错：

```python
if not is_memory_case and any(
    (
        self.required_memory_ids,
        self.recalled_memory_ids,
        self.missing_required_memory_ids,
        self.forbidden_memory_hits,
    )
):
    raise ValueError("non-memory Golden case result cannot contain history identities")
```

docstring 交代了动机："防止聚合分母被可选字段静默污染。"一条单组件案例如果不小心带上了 `required_memory_ids`，它就会悄悄进入历史召回覆盖率的分母，而没人会注意到这个分母从 3 变成了 4。

### 14.7.4 引用完整性：一个 v21 假阳性事故留下的两条规则

`golden_citation_completeness` 看起来是最简单的指标——检查关键结论有没有引用证据。它的实现却是这个文件里注释最长的一段，因为 v21 版本在这里出过一次假阳性：

```python
# 引用判定拆成两个独立问题，v21 把它们混成一个 AND 条件因此产生假阳性：
# 1) 悬空引用——引用了本次根本不存在的 ID。判定宇宙必须与报告层 `collect_reference_sources`
#    严格同源，因此这里直接调用那个生产函数，而不是在评测侧重新枚举一份容器清单。v21 漏掉了
#    Bundle 知识节点与文档切片，于是报告多引用一条合法的 `kn_*` 依据反而被记成悬空引用。
# 2) 实时支撑——关键结论不能只靠静态知识站住。因此额外要求至少一条引用落在本次 Observation、
#    可引用图路径或已确认历史案例上，与 Planner 把假设提升为 supported 的规则同源。
```

事故的形状值得记住：v21 在评测侧自己枚举了一份"合法引用 ID 清单"，漏了 Bundle 里的知识节点和文档切片。于是一份**引用得更充分**的报告（多引了一条 `kn_*` 知识依据）被判成引用了不存在的 ID。指标在惩罚正确行为。

修法不是补全那份清单，而是**删掉那份清单**，直接调用生产代码里的 `collect_reference_sources`：

```python
citable_refs = set(
    collect_reference_sources(
        state,
        diagnosis.evidence_bundle,
        tuple(
            match.memory
            for match in diagnosis.recalled_memories
            if match.memory.status is MemoryStatus.CONFIRMED
        ),
    )
)
```

这是评测代码的一条通用经验：**任何"什么算合法"的判定，只要生产代码里已经有一份权威定义，评测就不能有第二份**。两份定义一定会漂移，而漂移的症状是评测报告说谎——可能说系统更差（v21 这次），也可能说系统更好。

第二个判定集合是"实时支撑"，它比可引用集合窄得多：

```python
support_refs = {evidence.evidence_id for evidence in state.evidence}
support_refs.update(path.path_id for path in candidate_paths)
support_refs.update(match.memory.memory_id for match in diagnosis.recalled_memories)
```

于是一条关键结论要拿分必须同时满足三件事：

```python
unsupported_claims = sum(
    not refs
    or any(reference not in citable_refs for reference in refs)
    or not any(reference in support_refs for reference in refs)
    for refs in critical_claim_refs
)
```

有引用、每一条引用都不悬空、至少一条引用是本轮实时的。第三个条件封住的漏洞是"纯静态知识撑起一条关键结论"——只引知识图节点就宣布根因，这在形式上引用完整，实质上等于不看证据猜答案。

"关键结论"的范围也是显式定义的：所有根因、所有故障链步骤，加上**风险等级为 HIGH 的处置建议**。低风险建议不进这个分母；高风险建议必须有证据支撑。docstring 最后一句划清了这个指标的天花板：**"两者都只检查稳定 ID，不用字符串相似度替代 Auditor 或人工语义审查。"** 引用的内容到底支不支持结论，这个指标答不了——那是第 9 章的 Auditor 和人工抽查的活。manifest 的 note 也照实写着："只验证引用 ID 存在；证据语义支持度仍由 Auditor 与人工抽查负责。"

### 14.7.5 根因锚点：为什么要在文本相等之外再加一个指标

`golden_root_cause_top1` 判定的是最终报告的 Top-1 根因文本是否在 `allowed_root_causes` 里。这个判定有个明显的弱点：它衡量的是**措辞**。一个能背下标注答案措辞的模型可以拿满分，而它是否真的定位到了知识图上那个根因节点，这个指标看不出来。

于是 v24 加了一个并列指标 `golden_root_cause_anchor`。它的判定基础是一个巧合般好用的性质：

```python
# 根因锚点只看 top-1 根因这一条 claim 引用了哪些 `kn_root_cause_*`。之所以能纯离线精确判定，是
# 因为 Bundle 的知识节点 evidence_id 由 `app/retrieval/budget.py` 固定生成为 `kn_<node_id>`，一
# 条引用就精确编码了知识图节点 ID，不需要再对自然语言根因文本做相等或相似度比较。
```

`evidence_id` 的构造规则（`kn_` + 节点 ID）让一条引用字符串**精确编码了一个知识图节点**。所以"模型是否指向了正确的根因节点"这个问题可以用集合交集回答，不需要任何语义比较。

判定复用了 §14.7.4 的两道校验：

```python
anchor_claim_valid = (
    bool(top1_refs)
    and all(reference in citable_refs for reference in top1_refs)
    and any(reference in support_refs for reference in top1_refs)
)
```

注释写明了不加这两道会怎样：**"否则模型凭空编一个节点 ID，或只堆静态知识而不看本轮 Observation，都能刷出'命中正确根因'——这个指标就又一次退化成衡量措辞。"** 通过校验后才提取锚点：

```python
anchor_prefix = f"{KNOWLEDGE_EVIDENCE_ID_PREFIX}{ROOT_CAUSE_ANCHOR_NODE_PREFIX}"
cited_anchors = list(
    dict.fromkeys(
        reference.removeprefix(KNOWLEDGE_EVIDENCE_ID_PREFIX)
        for reference in top1_refs
        if reference.startswith(anchor_prefix)
    )
)
anchor_hit = bool(set(cited_anchors) & set(root_cause_anchors))
```

**关键在于两个指标的分母不同，而且必须并列上报。** 28 条案例里 21 条有标注根因（进 `golden_root_cause_top1` 的分母），其中只有 14 条在知识图里有对应的 `kn_root_cause_*` 节点（进锚点分母）。源码里两处注释都在强调这件事：

```python
# 锚点分母独立于文本相等分母：只有声明了锚点的案例进入，因此"知识图还缺这个根因节点"不会被
# 计成模型失败。两个比率必须并列上报，永远不能相减或互相替换。
```

manifest 的 note 也写着："与文本相等的 `golden_root_cause_top1` 是两个独立指标，分母不同，不可相减。"

为什么要专门警告"不可相减"？因为 1.0（21 条）和 1.0（14 条）看起来可以做算术，而实际上它们回答的是两个不同问题的两个不同子集。剩下那 7 条有根因但没锚点的案例，缺的是**知识图节点**而不是模型能力——把它们算进锚点分母，得到的 14/21 = 0.667 会被读成"模型有三分之一的情况找不到根因节点"，这是彻底的误读。§14.15 会把这 7 条列进遗留问题：要提高锚点覆盖，得去补知识图，不是去调模型。

分母为空时的约定在报告校验器里写死了：

```python
if self.anchored_case_count == 0 and self.root_cause_anchor_hit_rate != 1.0:
    raise ValueError("golden diagnosis empty anchor denominator must report 1.0")
```

而 `anchored_case_count` 本身必须能从明细复算，注释解释了为什么这件事重要：**"一旦被手工填成案例总数，读者就会把'14 条案例的比率'误读成'28 条案例的比率'。"**

### 14.7.6 故障链评分：`min(节点, 关系)`

`_score_fault_path_requirement` 对每条标注路径，在所有"已检索且已被报告引用"的候选路径里找最佳覆盖：

```python
node_coverage = _ordered_coverage(path.node_ids, requirement.required_node_ids)
relation_coverage = _ordered_coverage(
    path.relation_types,
    requirement.required_relation_types,
)
# 两类结构同时成立才是可解释故障链；取最小值相当于把较弱边界作为瓶颈。
scored_paths.append((path.path_id, min(node_coverage, relation_coverage)))
```

取最小值而不是平均值：节点全对但边类型错了，说明模型认对了参与者、认错了因果关系。"BDS 分区缺失 `CAUSES` LTS 阻塞"和"BDS 分区缺失 `DEPENDS_ON` LTS 阻塞"涉及的节点完全相同，但只有一个是正确诊断。平均值会给这种情况 0.5，最小值给 0——后者才对得上"可解释故障链"这个要求。

而且路径必须**同时**满足两个条件才进入候选：出现在本轮检索结果里，并且被最终报告的 `fault_chain` 引用了。检索到但报告没用的路径不算，报告引用了但检索里没有的路径是悬空引用。

`_citable_graph_paths` 那段注释记录了另一个和 v21 同类的口径事故风险：

```python
# 只看 state 会让 `fault_path_completeness` 在真实链路上恒为 0。
```

原因是生产运行时只填充 `EvidenceBundle.selected_paths`，`AgentState.retrieved_paths` 只在 checkpoint 恢复时才带回旧路径。评测如果只读后者，这项指标会在真实模型路径上永远是 0——**而这个 0 看起来完全像是"模型没有给出故障链"**。投影时还有一个细节：Bundle 路径的分数字段是 `hybrid_score` 而不是 `path_score`，注释解释了选择："混合分已含图结构与向量相似度，且值域与 `RetrievedPath.score` 一致；`path_score` 只是其中一个分量。"

### 14.7.7 五个容易读错的口径约定

这个文件里有五个小函数，每一个都在处理"没有数据时报什么"，值得单独列出来。

**一、`_mean` 空集合返回 1.0。**

```python
measured = [float(value) for value in values if value is not None]
return 1.0 if not measured else sum(measured) / len(measured)
```

docstring 里那句话是整个第 14 章的关键：**"该约定会在报告文档解释，不能把空类别的 1.0 当作有样本测得的能力值。"** 这个 1.0 的语义是"没有违反项"，不是"能力完美"。选它而不选 0.0 是因为安全类指标（安全降级率、冲突处置率）的分母天然可能为空——没有需要降级的案例时报 0.0 等于凭空制造一个失败。代价是读者必须能看到分母，所以 manifest 的每个 `treatment_label` 都带着样本数（`measured_scripted_7_cases`、`measured_scripted_14_anchor_cases`），这不是修饰，是读懂那个比率的必要信息。

**二、`_coverage` 空 required 返回 1.0**，理由相同："表示该案例没有此项义务，而不是向宏观结果额外注入一个失败。"注意它和 `_ordered_coverage` 的区别——后者空 required 直接抛错，因为它只被内部调用，Pydantic 已经保证非空，空输入意味着调用契约漂移了。**同一种"空输入"，在不同层次有不同的正确反应**。

**三、只有 `attempt == 1` 算逻辑动作。**

```python
# attempt=2 表示同一逻辑 Action 的受控瞬时重试；只有新的 attempt=1 才可能构成重复决策。
logical_action_keys = [
    _action_key(event.tool_name.value, event.request.model_dump_json())
    for event in state.tool_events
    if event.attempt == 1
]
```

第 5 章讲过瞬时错误最多重试一次且不增加 `react_step`。这里是同一个语义在评测侧的体现：重试是控制器行为，不是 Planner 又决策了一次。如果把 attempt=2 也算进去，`duplicate_action_rate` 会把正常的重试记成"模型重复调用同一个工具"。

**四、`_action_key` 不用 Python 哈希。** 用工具名 + 规范化 JSON 拼字符串，"避免进程随机种子影响结果"。评测结论必须在两次运行之间可比，而 `hash()` 对字符串在不同进程里返回不同值。

**五、`_highest_risk` 空建议列表返回 LOW。** docstring：无建议时按"只读/未处置语义归为低风险"。这条影响的是 `risk_level_hit_rate`——一份没有给出任何处置建议的报告，它的最高风险等级是 LOW 而不是未定义。

### 14.7.8 安全降级：空报告不算降级

七条案例没有标注根因（歧义、信息不足、工具全部不可用等），它们进入的是 `golden_safe_degradation` 的分母：

```python
safe_degradation = None
if not case.allowed_root_causes:
    # 安全降级必须同时克制根因输出并公开不确定性；仅返回空报告不算可解释降级。
    safe_degradation = not report.root_causes and bool(report.uncertainties)
```

**两个条件的合取**：没有猜根因，并且公开了不确定性。只满足前者的是一份沉默的报告——用户看不出系统是"查过了但证据不足"还是"根本没查"。安全降级的产品含义是**可解释地拒绝作答**，所以 `uncertainties` 非空是硬要求。

冲突处置那一条（唯一一条 `TOOL_ANOMALY_OR_CONFLICT` 冲突案例）的判定更严，注释点出了它要防的直觉错误：

```python
# "成功响应"本身不代表事实一致；安全通过必须先完整观察冲突双方，再克制结论并公开边界。
evidence_conflict_safe_resolution = (
    not missing_conflict_sources
    and not forbidden_conflict_root_hits
    and no_root_requirement_met
    and uncertainty_requirement_met
)
```

第一个条件是**先看到冲突**：标注的冲突证据来源必须全部出现在本轮 Observation 里。这条排除了一种假通过——模型只查了冲突的一边，因此没发现矛盾，于是给出了一个自信的答案。这种情况下"没有输出禁止根因"是运气，不是处置。manifest 的 note 记的就是这个：三个成功的 BDS Observation 全部保留，报告未选择任一禁止根因，并公开人工复核不确定性。

## 14.8 确定性 runner：用 Fixture 冒充模型，但不冒充口径

`tests/integration/test_golden_diagnosis_evaluation.py` 里的 `FixtureBackedGoldenRunner` 是第五层在无模型环境下的执行者。它跑真实的 `AuditedDiagnosisWorkflow`、真实的 MCP 子进程、真实的检索，只把 Planner 和 Auditor 换成确定性脚本。

它有一行值得单独看：

```python
components = tuple(dict.fromkeys(case.requested_components))
```

注释说明了理由：**"它与真实模型入口同源，两个 runner 不会在'该问哪些组件'上分叉。"** §14.10 会看到 `live_golden.py` 里对应的那一行。两个 runner 共用打分器（§14.7.1）还不够——如果它们在"输入怎么构造"上有分歧，确定性 1.0 和真实模型的分数之间仍然没有可比性。

这个测试文件的断言是本章确定性数字的来源：

```text
case_count == 28
category_case_counts == {8, 10, 4, 3, 3}
anchored_case_count == 14
intent_accuracy == 1.0
root_cause_top1_hit_rate == 1.0
root_cause_anchor_hit_rate == 1.0
necessary_action_coverage == 1.0
fault_path_completeness == 1.0
citation_completeness == 1.0
safe_degradation_rate == 1.0
tool_attempt_success_rate == pytest.approx(92 / 99)
```

最后一行是唯一一个不是 1.0 的数字，而它是**故意的**：99 次工具尝试里有 7 次失败，来自那些专门注入超时、权限拒绝、空结果的案例。如果这个数字是 1.0，说明工具异常那一类案例根本没在测异常。

文件的 docstring 保留了整章反复出现的那句限制：**"数量达标只证明当前确定性数据集完整，仍不能外推为真实 Planner/Auditor 模型能力。"** 28/28 这个数字唯一证明的事情是：数据集齐了、每条案例都能跑通、每个维度的打分逻辑都被执行过。它**不证明**模型好——因为这一轮里根本没有模型。

## 14.9 统一运行器：五个门禁挡住五种"报告说谎"

`app/evaluation/portfolio.py`（844 行）是 `python -m app.evaluation` 的实现。它自己不算任何指标——**它跑 pytest，然后决定哪些指标有资格被发布**。这个定位决定了它的代码形态：844 行里绝大部分是校验器。

### 14.9.1 版本钉住：manifest 不能挑一个好看的历史快照

文件开头是四张按 manifest 版本索引的字典，长得很笨，但每一张都在防一件事：

```python
_GOLDEN_SOURCE_CONTRACT_BY_MANIFEST = {
    "portfolio-eval-manifest:v2": "golden-diagnosis-eval:v1",
    ...
    "portfolio-eval-manifest:v24": "golden-diagnosis-eval:v23",
}
_GOLDEN_COVERAGE_VALUE_BY_MANIFEST = {
    "portfolio-eval-manifest:v2": 0.1786,
    ...
    "portfolio-eval-manifest:v22": 1.0,
    "portfolio-eval-manifest:v23": 1.0,
    "portfolio-eval-manifest:v24": 1.0,
}
```

第二张字典就是这个项目的 Golden 集增长史：0.1786（5/28）→ 0.2857 → … → 0.9643 → 1.0。每次加案例都升一版 manifest 并留下当时的覆盖率。为什么要留？因为 `validate_suite_coverage` 会**用它反查**：

```python
expected_coverage = _GOLDEN_COVERAGE_VALUE_BY_MANIFEST[self.contract_id]
if (
    coverage_metric is None
    or abs(coverage_metric.treatment_value - expected_coverage) > 1e-4
):
    raise ValueError(
        f"{self.contract_id} requires Golden coverage snapshot {expected_coverage}"
    )
```

于是 `portfolio-eval-manifest:v24` 这个字符串**就是一份声明**："这份报告的 Golden 集是 28/28。"想在 manifest 里填一个别的覆盖率，就必须换版本号；换了版本号，`_GOLDEN_REQUIRED_METRIC_IDS_BY_MANIFEST` 会要求那一版对应的指标集合精确匹配（v24 强制包含 `golden_root_cause_anchor`），`_GOLDEN_SOURCE_CONTRACT_BY_MANIFEST` 会要求源契约对得上，而这些常量又被 `tests/unit/test_documentation_policy.py` 的字面量断言和 `app/core/settings.py` 的期望值锁住（CLAUDE.md 里那条"契约 ID 必须多处同步"）。

一条改动要同时穿过五个地方才能生效，这在日常开发里是负担。但对评测报告，这个负担正是它的价值：**没有一处可以单独修改**。

`validate_suite_coverage` 还要求 suite 集合**精确等于**批准集合（不是包含关系），以及 metric ID 全局唯一：

```python
if set(suite_ids) != required_suite_ids:
    raise ValueError(
        f"{self.contract_id} must contain exactly its approved evaluation suites"
    )
```

精确相等挡住的是"临时注释掉一层"——少一层不是"报告少了点内容"，而是 manifest 加载失败。

### 14.9.2 `test_targets` 是路径白名单，不是命令行

manifest 是一份 JSON，里面写着每层要跑哪些测试。这意味着 JSON 里的字符串会变成子进程参数——一个典型的注入面：

```python
_TEST_TARGET = re.compile(r"^tests/[a-zA-Z0-9_./-]+\.py(?:::[a-zA-Z0-9_\[\]-]+)?$")
```

正则要求：必须以 `tests/` 开头、必须以 `.py` 结尾、可选一个 `::测试名` 后缀，字符集限死。它同时挡住三类东西：以 `-` 开头的 pytest 参数（比如 `-p no:cacheprovider`，或者更糟的 `--deselect`）、shell 元字符、以及指向 `tests/` 之外的路径。

命令的构造方式是另一半保障：

```python
command = [sys.executable, "-m", "pytest", "-q", *targets]
```

加上执行器里的 `shell=False`。docstring 把两层关系写清了："target 必须匹配受限仓库路径，不允许以 `-` 开头或插入 shell 字符；执行器随后仍使用 `shell=False`。"

这个防御值得解释一下动机，因为 manifest 是仓库自己的文件，不是外部输入。但它是**数据文件**，而数据文件会被 PR 修改、会被脚本生成、会从别的分支合过来。一个能在 manifest 里塞 `--deselect` 的人可以让某层"通过"而实际上跳过了失败的用例，报告仍然是 `passed` 并附带指标。用一个正则把这条路封死，成本是一行。

超时也有明确语义：

```python
except subprocess.TimeoutExpired:
    return PytestExecutionResult(
        return_code=124,
        duration_ms=duration_ms,
        output_summary=f"pytest exceeded {self._timeout_seconds} seconds",
    )
```

返回 124（GNU `timeout` 的约定）而不是抛异常，"使其他 suite 仍可运行并在最终报告中标记失败"。超时的那一层拿到非零退出码 → 状态是 `failed` → 不携带指标 → `run_success=false`。**一层卡住不会让另外四层的结果丢失，但也绝不会让这一层混过去。**

### 14.9.3 四态状态机：`skipped` 和 `blocked` 必须是两个词

```python
class SuiteExecutionStatus(StrEnum):
    """区分单层测试通过、失败、主动跳过和缺少前置四种结果。

    skipped 只来自显式快速模式；blocked 表示用户请求完整运行但缺数据库。两者都不携带旧指标，
    failed 表示 pytest 已执行但未通过，passed 才允许发布 manifest 快照。
    """
```

§14.1 那两次实测的区别就在这里。同样是"两层没跑"：

| 命令 | 状态 | 退出码 | `run_success` | `complete` |
|---|---|---|---|---|
| `--skip-postgres` | `skipped` | 0 | true | false |
| 无参数（无测试库） | `blocked` | 1 | false | false |

为什么不能合成一个词？因为**用户意图不同**。`--skip-postgres` 是显式声明"我现在只要快速反馈"，这时退出码 0 是正确的——它没有失败，只是不完整。而不带参数运行意味着用户要一份完整报告，缺数据库就是没做到，退出码必须非 0，否则 CI 会把一份缺两层的报告当成成功。

两种状态的**共同点**同样重要：都不携带指标。这条由 `validate_status_payload` 强制：

```python
if self.status is SuiteExecutionStatus.PASSED:
    if not self.metrics or self.failure_summary is not None:
        raise ValueError("passed portfolio suite requires metrics and no failure summary")
    return self
if self.metrics or self.failure_summary is None:
    raise ValueError("non-passed portfolio suite must hide metrics and explain the status")
```

docstring 叫它"防止失败后仍宣传旧百分比的最后一道结构化门禁"。这解释了 §14.1 里那两个 `metrics=0`：manifest 里明明存着 GraphRAG 那两个指标的历史值（1.0、1.0），但这一轮没跑，所以报告里一个都不出现。**"上次测出来是 1.0"不是这一轮的结果。**

反向约束（passed 必须有指标）挡的是另一种情况：一层测试跑过了但 manifest 里忘了写指标，报告会显示一个空的 `passed` 层，读者以为这层没什么可测的。

### 14.9.4 三个汇总布尔值必须能被重算

`run_portfolio_evaluation` 算完三个布尔值放进报告，`validate_summary_flags` 立刻把它们重算一遍再比对：

```python
expected_success = not any(
    status in {SuiteExecutionStatus.FAILED, SuiteExecutionStatus.BLOCKED}
    for status in statuses
)
expected_complete = not any(
    status in {SuiteExecutionStatus.SKIPPED, SuiteExecutionStatus.BLOCKED}
    for status in statuses
)
expected_all_passed = all(status is SuiteExecutionStatus.PASSED for status in statuses)
if self.run_success != expected_success:
    raise ValueError("portfolio run_success does not match suite statuses")
```

同一份逻辑写两遍看着很蠢——生产者和校验器紧挨着，中间没有网络也没有序列化。但这三个布尔值是**整份报告最容易被单独消费的字段**：CI 只看 `run_success`，README 只引 `all_suites_passed`。它们和逐层状态之间的一致性因此是最值得用类型保护的一条，而不是最不值得的。docstring 的说法是"拒绝调用方手工美化结果"，其中"调用方"也包括未来那个想在报告构造处加一个 `or force_success` 的自己。

注意三个布尔值的定义各不相同，覆盖三个不同的问题：

- `run_success`：这次运行有没有出错（failed 或 blocked）。
- `complete`：这份报告完整吗（有没有 skipped 或 blocked）。
- `all_suites_passed`：五层是不是全绿。

`--skip-postgres` 那次实测的组合是 `true / false / false`——**成功但不完整**，这正是快速模式该有的自我描述。任何把它们合并成一个"成功"字段的做法都会丢掉这个区分。

### 14.9.5 一行 Windows 妥协

```python
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
```

Windows 终端默认 GBK 代码页，而 manifest 的指标说明全是中文。不 `reconfigure` 的话，把 JSON 报告重定向到文件会得到一堆 `UnicodeEncodeError` 或者乱码。写在 `hasattr` 后面是因为 `sys.stdout` 被替换成不支持该方法的对象（测试替身、某些 CI 采集器）时不能崩。

本章写作过程中就踩了这个坑的另一面：用 `python -c` 打印 manifest 的中文标签时终端输出乱码，只能改成写 UTF-8 文件再读。§14.1 那些命令输出之所以能正确显示中文，靠的就是这两行。

## 14.10 真实模型入口：付费之前先失败

`app/evaluation/live_golden.py`（523 行，`live-golden-eval:v3`）是唯一会产生真实费用的评测入口。它的设计目标只有一个：**任何配置错误都必须在第一次付费调用之前暴露**。

### 14.10.1 预检顺序

```python
if settings.chat_provider is ChatProvider.DISABLED:
    raise LiveGoldenSetupError("live Golden evaluation requires a real chat provider")
if not settings.chat_api_key:
    raise LiveGoldenSetupError("live Golden evaluation requires DATAOPS_CHAT_API_KEY")
if not settings.database_url:
    raise LiveGoldenSetupError("live Golden evaluation requires DATAOPS_DATABASE_URL")
```

三条检查按"越便宜越靠前"排：Provider 是否启用（读配置）、API key 是否存在（读配置）、数据库 URL 是否存在（读配置）。全部在 FastAPI 应用启动之前，更在任何模型调用之前。

§14.1 那次实测触发的是第三条——`.env` 已经提供了 Provider 和 key，但本机没配 `DATAOPS_DATABASE_URL`。错误信息是 argparse 的两行短消息，不是堆栈：

```python
except LiveGoldenSetupError as error:
    parser.error(str(error))
```

`parser.error` 打印 usage 加一行原因，然后退出码 2。**配置错误和运行时错误的表现形式必须不同**：配置错误是"你还没准备好"，一行话说清就够了，堆栈只会淹没那句话；而运行时错误（Provider 返回 500、数据库连接中断）需要完整堆栈来定位。所以后者不被 `except` 捕获。

`--all-cases` 与 `--case-id` 的互斥检查放在这三条之后：

```python
if case_id and all_cases:
    raise LiveGoldenSetupError("choose either --case-id or --all-cases, not both")
```

顺序是有意的。参数用错和环境没配都是配置错误，但先报环境问题更有用——一个把两个参数都写上的用户，如果环境也没配好，修完参数还要再撞一次墙。

（源码里三条的原文是 `settings.chat_provider == "disabled"`、`settings.chat_api_key is None`、`settings.database_url is None`，错误消息分别点名 `DATAOPS_CHAT_PROVIDER`、`DATAOPS_CHAT_API_KEY`、`DATAOPS_DATABASE_URL`——每条都直接告诉用户该设哪个环境变量，而不是笼统地说"配置不完整"。）

### 14.10.2 三档口径：`smoke` / `full` / `custom`

真实模型评测很贵，所以日常只跑一条案例冒烟。于是报告必须能区分"跑了一条"和"跑了 28 条"，否则两个 JSON 长得一样：

```python
def resolve_live_golden_scope(
    case_ids: Sequence[str],
    all_case_ids: Sequence[str],
) -> LiveGoldenScope:
```

判定规则是：与默认冒烟序列**逐位相等**才是 `smoke`；与全集**集合相等且包含全部案例 ID** 才是 `full`；其余一律是 `custom`。`build_live_golden_report` 的 docstring 点出了它防的误读："28 条全集运行不会与冒烟运行共用同一个口径标签。"

这是"分母必须能从报告里读出来"这条原则在真实模型侧的实现。一份 `scope=smoke` 的报告里如果出现 `root_cause_top1_hit_rate=1.0`，读者立刻知道那是 1/1；而同样的 1.0 出现在 `scope=full` 里才是 21/21。**同一个数字，不同的分母，不同的可信度**——标签让这个区别无法被忽略。

### 14.10.3 输入构造：只加路由元数据

```python
def build_live_golden_message(
```

真实模型跑 Golden 案例时，光有用户提问不够——工具需要知道该查哪个资源。但**往提问里塞标注信息就是作弊**：如果消息里写了"根因是 BDS 分区缺失"，那测的就不是诊断能力了。

所以这个函数只追加一段带明确标记的路由元数据（`[合成评测路由元数据]`），内容限于 `scenario_id`、资源 ID 和观察窗口——都是"去哪儿查"，没有一个字涉及"答案是什么"。组件列表的构造和确定性 runner 同源：

```python
components = tuple(case.requested_components)
```

注释解释了为什么不能从 fixture 反推组件：几条单组件案例共用一个三组件 fixture，按 fixture 推会让 Planner 拿到比标注更宽的组件范围，capability 门禁的行为就跟着变了。§14.8 那行 `dict.fromkeys(case.requested_components)` 是同一个决定的另一半。

### 14.10.4 recorder 与 Worker 的一个陷阱

第 11 章讲过 run 由 PostgreSQL Worker 执行。真实评测要记录 token 消耗和耗时，用的是第 12 章那个 `model-call-metric:v1` ContextVar recorder。这两件事凑在一起有个陷阱：**ContextVar 只沿 `asyncio` 任务树继承**。如果 Worker 在自己的后台任务里执行 run，那个任务不是评测协程的子任务，recorder 就收不到任何数据——报告里的 token 数会是 0，而且看起来完全正常。

解法是评测自己驱动 Worker：

```python
await worker.stop()
...
await worker.run_once()
```

先停掉后台循环，再由评测协程直接调 `run_once()`。这样执行 run 的任务确实是评测协程的子任务，recorder 正常继承。清理放在 `finally` 里：

```python
finally:
    reset_model_call_recorder(token)
```

docstring 写的理由是"避免污染同进程后续任务"。ContextVar 的 token 不重置，同进程里后续的诊断请求会继续往这个评测的 recorder 里写数据。

### 14.10.5 基础设施错误绝不记成模型零分

```python
snapshot = await self._client.get_run(run_id)
if snapshot is None:
    raise RuntimeError(...)
```

`LiveGoldenRunner.run` 对四种情况抛异常而不返回一个零分结果：run 快照拿不到、run 还在执行、run 状态是 failed、run 完成了但没有结果。

这是 §14.2 第三条约束最实际的一次应用。真实模型评测的失败原因分布很宽：Provider 限流、网络超时、数据库连接池耗尽、MCP 子进程被杀。这些全都可能在一次 28 条案例的运行中间发生。如果它们被记成 0 分案例，那么 `root_cause_top1_hit_rate` 这个数字就同时受两个因素影响——模型能力和网络质量——而报告的读者会把它整个理解成前者。

宁可整轮失败重跑（重跑要再花一次钱），也不发布一份混着基础设施噪声的分数。

### 14.10.6 `--seed-history`：给四个记忆指标补上分母

`live-golden-eval:v3` 相对 v2 只加了一件事，而这件事来自 §14.12.1 那条最难看的自我批评：确定性 runner 把 Golden 标注直接投影成 `CaseMemoryMatch`，live runner 走的是生产 confirmed-only 检索路径，而真实库里一条 confirmed 案例都没有。于是四个记忆指标必然是 0——**不是模型没做到，是这四项没测**。

补分母的模块是 `app/evaluation/live_history_seed.py`（264 行）。它最值得学的地方不是"往表里插几行数据"，而是它拒绝走的那几条捷径。

**第一，confirmed 状态不许自己写。** 模块只声明两个协议方法：

```python
class SeededMemoryRuntime(Protocol):
    async def delete(self, memory_id: str) -> CaseMemory | None: ...
    async def decide(self, memory_id: str, decision: MemoryDecision) -> CaseMemory | None: ...
```

插入阶段所有行一律写 `MemoryStatus.PENDING`，之后逐条调用 `decide(memory_id, MemoryDecision.CONFIRM)`。为什么不直接 `INSERT ... status='confirmed'`？因为第 10 章讲过，confirm 是一个**事务**：它更新状态、注册动态 case 图节点、按独立阈值建立双向 `SIMILAR_TO` 边。直接写一行 confirmed 会得到一条"向量能召回、图通道召回不到"的畸形数据，而 `confirmed_only_recall_rate` 恰好要验证图通道。绕过状态机就等于把被测对象换掉了。

**第二，forbidden 记忆必须真的相似。** 一个记忆案例的 `history_expectation` 有两部分：`required_memories`（必须召回）和 `forbidden_memory_ids`（不得召回）。后者的行症状取的是**声明它的那些案例的原始用户问题**：

```python
forbidden_symptoms.setdefault(memory_id, []).append(f"历史合成症状：{case.user_query}")
```

如果给 forbidden 行编一段无关文本，向量本来就不相似，"非 confirmed 不得被召回"这条门禁就永远是空过——测的是余弦距离，不是状态过滤。让它们和查询足够像，SQL 里那个 `status = 'confirmed'` 条件才是唯一把它们挡住的东西。

同一个原因还决定了状态怎么分配：

```python
target_status=(MemoryStatus.PENDING if order % 2 == 0 else MemoryStatus.REJECTED)
```

两个 forbidden ID 分别落在 pending 和 rejected。都写成 pending 的话，rejected 分支在整轮 live 评测里一次都不会被执行。

**第三，向量必须落在同一个数学空间。** 预置复用了生产的嵌入文本构造函数——这也是本次改动把 `app/memory/service.py` 里的私有 `_memory_text` 改名成公开 `memory_embedding_text` 的原因：

```python
texts = [memory_embedding_text(row.memory) for row in rows]
vectors = await embedding_provider.embed_texts(texts)
```

`embedding_provider` 是从 `app.state.embedding_provider` 取的，也就是检索时用的那一个实例。如果预置自己 `new` 一个 Provider，Provider ID 或维度只要差一点，pgvector 就永远召回不到预置数据——**而那种失败看起来和"模型没召回"一模一样**。报告里的 `history_seed` 因此同时记录 Provider ID 和维度，让读者能自己排除这种可能。

**第四，顺序和事务边界。** 流程固定为"按 ID 删除旧行 → 单事务批量插入 pending → 逐条走生产决策"：

```python
for row in rows:
    await memory_runtime.delete(row.memory.memory_id)

async with session_factory.begin() as session:
    ...
```

删除在前，重复运行才不会撞签名唯一约束，也不会把上一轮的 occurrence 计数和图边继承进来。插入共享一个事务，任何一行失败就整批回滚——半套历史数据会让召回率变成一个无法解释的中间值，比直接失败更难查。签名还额外混入了 `memory_id` 和一个命名空间常量，因为 Golden 标注里多条历史案例完全可能共享同一组件与根因文本，而 `find_exact` 是按签名精确去重的。

**第五，这个开关默认关闭，而且它改变口径。** `--seed-history` 是 `action="store_true"`，默认 False。两个理由：它会真实写库（默认打开等于替使用者决定数据库内容，事后谁都分不清某条 confirmed 是评测放进去的还是用户确认的）；更重要的是，非冲突记忆案例的 `historical_root_cause` **就等于本次的正确根因**。这不是泄漏——它是 Golden 标注刻意设计的输入，冲突案例的历史根因故意与本次不同，用来测冲突保护。但它意味着**开了预置的运行和 Run A–H 不能放在同一列比较**，记忆类案例的根因指标分母口径已经变了。报告里的 `history_seed` 字段就是这个提醒的载体：

```python
# None 表示本轮没有预置历史案例：数据库里没有 confirmed 案例时四个记忆指标必然是 0，那是前置
# 条件缺失而不是模型表现。字段可选而不是必填，是为了让 v1/v2 时代已发布的运行仍能被原样解释。
history_seed: LiveHistorySeedReport | None = None
```

预置的调用位置也是设计的一部分——它在 `worker.stop()` 之后、计时和第一次付费调用之前。写库失败要以异常终止整轮，而不是先烧掉模型费用再拿到一份记忆指标全为 0、且无法判断原因的报告。

最后一句诚实声明：**截至本书写作时，这个机制没有跑过一次。** 机制存在不等于指标已测量，四个记忆指标至今没有实测值。

## 14.11 一次真实事故：28 条案例跑完之后才崩

这是全书里我最想让读者记住的一个坑，因为它便宜的时候几乎不可见，贵的时候一次就要重新付一遍钱。

现象：一次 28 条案例的真实模型评测**全部执行完毕**，在构造最终报告对象时抛出 `PydanticUserError`，付费结果一条没落盘。

原因链有四环，缺一环都不会触发：

1. `from __future__ import annotations` 让所有类型注解变成字符串，延迟求值。
2. Pydantic 建类时要解析这些字符串，它按 `cls.__module__` 去 `sys.modules` 里回查命名空间。
3. `python -m app.evaluation.live_golden` 走 `runpy`，模块代码跑在一份名为 `__main__` 的命名空间里，但 `sys.modules["__main__"]` **不是**这份命名空间。于是 `Literal` 之类的符号解析不到，Pydantic 不报错，而是把 core schema 推迟成 mock。
4. mock 的 schema 在**第一次实例化**时才尝试补建。而 `LiveGoldenEvalReport` 恰好是整轮跑完之后才构造的对象。

四环凑齐的效果是：一个纯粹的导入期类型问题，被推迟到了几十分钟、几十次付费调用之后才爆炸。而且它在 pytest 下永远不会出现——测试里模块是正常 import 的，`cls.__module__` 是真实模块名，一切正常。

修法是两行：

```python
LiveGoldenEvalReport.model_rebuild()
```

（`portfolio.py` 里有对应的六行，把它那六个模型都补建一遍。）注释把代价关系写得很直接：**"导入期补建把这类失败从'整轮结果作废'降级为'进程起不来'。"**

这句话是这一节的全部要点。同一个 bug，暴露时机决定了它的成本：进程起不来，损失 0；跑完才崩，损失一整轮真实模型评测的钱和时间。**任何"迟到的失败"都值得花代码把它提前**，评测入口尤其如此，因为它的执行成本最高。

`tests/unit/test_entrypoint_model_rebuild.py`（94 行）把这条约束固化了。它没有维护一张入口模块清单，而是 AST 扫描：

```python
modules = [
    path
    for path in sorted(APP_ROOT.rglob("*.py"))
    if any(_is_main_guard(node) for node in ast.parse(path.read_text(encoding="utf-8")).body)
]
assert modules, "no __main__ entrypoint modules were discovered under app/"
```

docstring 说明了为什么要动态发现：**"新增一个 `-m` 入口时本测试自动覆盖它，否则这条约束会退化成'只保护当年写下的两个文件'，而入口模块恰恰是最容易在几个月后被复制出第三个的地方。"**

然后它精确复现出事故的命名空间条件：

```python
tree.body = [node for node in tree.body if not _is_main_guard(node)]
namespace: dict[str, object] = {"__name__": "__main__", "__file__": str(path)}
exec(compile(tree, str(path), "exec"), namespace)  # noqa: S102
```

剔掉 `if __name__ == "__main__":` 守卫（否则会真的去连数据库、调模型），然后在 `__name__ == "__main__"` 但 `sys.modules["__main__"]` 指向别处的命名空间里执行模块源码——正是 `runpy` 的实测形态。最后断言：

```python
incomplete = sorted(
    model.__name__ for model in models if not getattr(model, "__pydantic_complete__", False)
)
assert not incomplete, (
    f"{path.name} defines Pydantic models that are not fully defined when the module runs as "
    f"__main__: {incomplete}; call model_rebuild() at module level"
)
```

失败消息直接给出修法（`call model_rebuild() at module level`），而不是只说哪里错了。一个几个月后才会被触发的测试，它的失败消息是唯一的现场文档。

§14.1 那句 `15 passed in 0.83s` 里就包含这个文件的用例——它按入口模块参数化，目前覆盖两个入口。

## 14.12 真实模型的实测数字：一份不好看的成绩单

前面十一节讲的都是确定性评测——满分、Δ 漂亮、跑得快。这一节讲真实模型跑出来的数字，它们不好看，而且**这才是这套评测存在的理由**。

所有数字来自 `docs/live-golden-eval-results.md`，全部是三案例 smoke（`scope=smoke`、`case_coverage_rate=0.107`）。固定条件：同一份三案例、同一 `bge-m3:v1` 向量空间、同一 `auditor-report:v2`、同一 `gpt-5.6-sol` 端点；唯一变量是 Planner Prompt 版本与实现修复。

| 指标 | Run B `planner-react:v6` | Run C `v7` | Run D `v8`+定位修订 | Run F `model-transient-retry:v1` |
|---|---|---|---|---|
| `intent_accuracy` | 1.000 | 1.000 | 1.000 | 1.000 |
| `necessary_action_coverage` | 0.944 | 0.778 | **1.000** | 0.833 |
| `evidence_source_coverage` | 0.944 | 0.778 | **1.000** | 0.833 |
| `fault_path_completeness` | 0.000 | 0.167 | **0.667** | 0.667 |
| `stop_reason_hit_rate` | 0.000 | 0.667 | 0.667 | 0.667 |
| `risk_level_hit_rate` | 0.333 | 0.333 | **0.667** | 0.667 |
| `accepted_report_rate` | 0.333 | 0.667 | 0.667 | 0.667 |
| `root_cause_top1_hit_rate` | 0.000 | 0.000 | 0.000 | 0.000 |
| `citation_completeness` | 1.000 | 1.000 | 1.000 | 1.000 |
| `duplicate_action_rate` | 0.000 | 0.000 | 0.000 | 0.000 |
| `tool_attempt_success_rate` | 0.917 | 0.818 | 0.857 | **1.000** |
| `safe_degradation_rate` | 1.000 | 1.000 | 1.000 | 1.000 |
| 模型调用次数 | 15 | 15 | **12** | 13 |
| 总 token | 135,685 | 151,199 | **126,670** | 189,996 |
| 三案例总耗时 | 275.6 s | 209.7 s | **175.2 s** | 227.2 s |

几个值得读的地方：

**Run C 是一次退步，而它被完整保留了。** `necessary_action_coverage` 从 0.944 掉到 0.778，原因是 v7 的 Planner 白名单比 `collect_reference_sources` 更窄，模型引用 Prompt 里刚给出的 `kn_*` 知识证据反而被控制器整批拒绝（第 6 章那条"整批拒绝而不截断"）。这和 §14.7.4 的 v21 事故是**同一个病**：同一个概念在两处各有一份定义。Run D 把两侧统一后回到 1.000。

**Run D 的延迟是这个项目最明确的一个未达标项。** 实测（单调时钟）：Planner 七次调用 10.3–15.9 s（中位 12.6 s），Auditor 五次 7.2–18.7 s（中位 16.3 s），三案例平均端到端约 58 s。产品设计的目标是 P95 ≤ 30 s。文档里的结论写得很直接：当前第三方端点的**单次** Planner/Auditor 延迟就已经占满预算，因此不能宣称达成。并行工具调用（第 6 章）只压缩了 MCP 等待时间，模型串行思考时间仍是主要成本——这是"并行只买延迟，不买预算"那条边界的另一面：它也买不到模型思考时间。

**Run E 测到的是配额，所以整轮不发布。** 同一端点在约 92 s 内成功完成 6 次调用，随后连续 4 次返回 HTTP 错误，每次只用 0.26–0.63 s 就被驳回——响应时间差两个数量级，说明后 4 次根本没打到模型。两个案例以 `planner_provider_error` 终结，`necessary_action_coverage` 直接归零。这些 0.667 / 0.333 全部作废，只保留成因记录。这是 §14.2 第三条约束在真实场景的第一次触发：**测到端点配额不等于测到模型质量**。

**Run F 加了重试，但它自己承认没测到重试。** 13 次调用全部成功，`tool_attempt_success_rate=1.000`。文档里那句话值得抄下来："**Run F 没有触发任何重试**……这份报告只能证明'加了重试之后链路仍然正常'，不能作为'重试在生产端点上成功救回了调用'的证据。"

真实证据来自 Run G——那次端点中途不可用，14 次调用里 6 次以 `timeout` 或 `connection_error` 结束，构成三组"首次调用 + 一次重试"，每组都在重试后仍失败。它同时证明了三件事：有界重试确实在真实端点上执行过、上限确实是一次、端点持续不可用时不会退化成无限重试。**一个功能的正面证据有时只能靠一次故障拿到。**

### 14.12.1 Run H：第一次跑完 28 条，以及它到底测到了什么

`golden-case:v9` 修好组件范围契约之后，`--all-cases` 第二次尝试跑完了全部 28 条案例，这是第一组 `scope=full` 实测数字：`case_coverage_rate=1.000`、`target_coverage_complete=true`、类别配额 8/10/4/3/3 与目标一致、142 次模型调用、1,306,915 token、71.2 分钟（均摊 152.5 s/案例）。

然后是必须与这些数字绑在一起读的那一半：**142 次调用只有 92 次成功**，44 次 `timeout`、3 次 `connection_error`、3 次 `output_invalid`；28 条案例里 11 条以 `planner_provider_error` 结束，其中 9 条**一个工具都没执行**。所以 `fault_path_completeness=0.300`、`necessary_action_coverage=0.702`、`accepted_report_rate=0.464` 这些数字，大部分测的是"这一轮有多少案例走到了终点"，不是"模型判断得多准"。

这一轮最有价值的产出不是任何一个比率，而是三个**用小样本 smoke 测不出来的**结构性发现：

1. **Planner 的超时配置和这个模型的响应分布是同一个量级。** 成功的 49 次 Planner 调用耗时中位 15.3 s、最大 **29.9 s**，而 `DATAOPS_CHAT_TIMEOUT_SECONDS=30`——分布的右尾正好压在墙上，33 次超时全部落在 30.0–31.7 s。Auditor 同形：成功中位 24.7 s、最大 89.2 s，11 次超时落在 90.0–90.4 s（配置 90 s）。三案例 smoke 里这个尾巴只有几次采样，看不出是配置问题还是端点问题；28 条 × 5 次调用才让它变成一条清晰的截断分布。
2. **记忆类别的四个指标在真实链路上什么都没测到，而且原因在评测入口。** `history_trigger_hit_rate=1.000`（三条案例都触发了召回），但 `history_recall_coverage` / `confirmed_only_recall_rate` / `history_projection_pass_rate` / `realtime_priority_pass_rate` 全是 0.000。原因不是模型忽略历史：真实库里只有 7 条 pending 记忆、**没有任何 confirmed 案例**，而 Golden 要求召回 `mem_golden_*_history`。确定性 runner 把 Golden 标注投影成 confirmed 匹配（§14.8），真实 runner 走生产召回路径，于是必然为空——连那条链路完全正常的 `golden_memory_flashsync_stable_reference`（3 个工具、`evidence_sufficient`）也是 0.000。这正是 §14.8 那条"用 Fixture 冒充模型，但不冒充口径"的反面教训：**替身补上的前置数据，真实运行时必须有人显式补上，否则指标会静默地测成 0。** 这条教训后来变成了 §14.10.6 的 `--seed-history`。
3. **`RiskLevel.HIGH` 在真实报告里一次都没被观测到。** `risk_level_hit_rate=0.500`，14 条未命中案例的实测等级**全部是 low**（3 条期望 high、11 条期望 medium）。`graph-seed:v12` 解除的是"HIGH 在生产路径不可达"这个实现上限（§14.14），但可达不等于被选中——一个案例的实测等级取被召回方案节点的最大值。

有三个数字在这一轮是可以正面陈述的，而且它们的共同点很说明问题：`duplicate_action_rate=0.000`、`safe_degradation_rate=1.000`、`forbidden_conflict_root_hit_count=0` 与 `forbidden_memory_hit_count=0`——**28 条案例上守住的全部是确定性规则负责的门禁**，模型质量相关的指标则被端点状况压得看不清。这恰好是第 9 章那条"确定性规则对模型有非对称否决权"的价值：模型可以不稳定，安全边界不能跟着不稳定。

最后一件事：这一轮的低分**不许**在调整超时配置之后被"追认"为改善。文档里写的是——任何调整之后本轮全部数字作废、必须重测。预期的改善不是成绩。

## 14.13 那个 0：两条不诚实的路，和唯一一条诚实的路

`root_cause_top1_hit_rate` 在 Run B、C、D、F、G 五次三案例运行和 Run H 的 28 条全量运行里，全部是 **0.000**（Run H 的分母是 21 条有根因案例）。

先说清这个 0 是什么。Run D 的案例 1，模型输出的根因文本准确说明了 `partition_date` 用了 `20260713` 而任务声明要求 `yyyy-MM-dd`——**这是正确的诊断**。而评分器用**精确字符串相等**把报告根因与知识图节点名比较。一段自然语言句子永远不可能等于 `root_cause_lts_invalid_partition_parameter` 这样的节点 ID。

所以这个 0 的准确含义是：**"模型的根因文本没有与标注答案字符串相等。"** 它不含任何关于模型是否找对根因的信息。

摆在面前有三条路。

**第一条不诚实的路：把 0 说成"模型找不到根因"。** 这是照字面读指标名（`root_cause_top1_hit_rate`），不看口径。它错在把一个测量措辞的指标当成了测量能力的指标，而且这个错误方向是**低估**系统——听起来很谦虚，实际上是在用一个错误的数字下结论。

**第二条不诚实的路：悄悄改口径让 0 变成非零。** 把精确相等换成子串匹配、或者换成向量相似度，`root_cause_top1_hit_rate` 立刻能报出一个 0.667 之类的数字。README 里那一行不用改指标名，也不用改历史表格——只有评分器变了。这是更严重的一种，因为它让所有历史数字失去可比性，而**读者完全看不出发生过什么**。

**第三条路：加一个分母不同的新指标，与旧指标并列，旧口径一个字不改。**

这就是 `golden_root_cause_anchor` 的来历（§14.7.5）。它判定的是 Top-1 根因引用集合里有没有对应的 `kn_root_cause_*` 节点，判定基础是 `evidence_id` 的构造规则而不是文本比较。Run G 实测 **0.500，分母 `anchored_case_count=2`**。

Run H 把这个指标第一次放到完整分母上：**0.071，分母 `anchored_case_count=14`**，唯一命中的是 `golden_bds_data_skew_single`（Top-1 引用了 `kn_root_cause_bds_data_skew`，同一条结论的 `root_cause_top1_hit` 仍是 false）。这里要克制两种读法：它**不是** Run G 的 0.500 "退步"了——两轮的分母是 2 与 14、样本口径是 smoke 与 full、而且 Run H 有 11 条案例根本没走到根因输出；它也**不是**"模型只有 7% 能力"——同一轮里 9 条案例零工具结束。分母不同、运行条件不同的两个数字放在一起时，唯一诚实的动作是把两者的分母和运行条件都写出来，然后**不做减法**。

Run G 案例 1 是这个设计在真实报告上的第一次验证：**`root_cause_top1_hit=false` 与 `root_cause_anchor_hit=true` 同时成立。** 同一份报告、同一个根因，两个指标给出相反的判定，而两个判定都是对的——文本确实不相等，节点确实被引用了。

这条路的关键在于它**必须付的三笔代价**，一笔都不能省：

1. **旧指标不许改，也不许消失。** Run G 的 `root_cause_top1_hit_rate` 照旧写 0.000。文档里那句话是这一节的核心：**"这不是把 0.000 变成 0.500 的提升。"**
2. **两个指标的分母必须同时可见。** Run G 里是 2 对 2，28 条全集里是 14 对 21。分母不同意味着这两个比率**不可相减**，源码注释、manifest note、报告校验器三处都在重复这句话。
3. **真要提升旧指标，必须显式改口径并作废旧数字。** 文档写明了那意味着什么："必须重新标注全部案例并作废旧数字，而不是用锚点悄悄替换。"

Run G2 还留下一个必须一起读的约定：它的 `root_cause_anchor_hit_rate` 显示 1.000，而 `anchored_case_count=0`。这就是 §14.7.7 那个 `_mean` 空分母返回 1.0 的约定在真实报告里的样子——**"本轮没有可判定案例"而不是满分**。看锚点比率必须同时看分母，而报告层的不变量强制这个分母从案例明细复算（§14.7.5），正是为了让这种误读在数据里就能被发现，而不是只在文档的一句注意事项里。

这一节可以浓缩成一条可迁移的规则：**当一个指标的口径有问题时，不要修那个指标，要在它旁边加一个新指标，并且让两个分母都露在外面。** 修口径会让历史失效且不留痕迹；并列发布让读者自己判断哪个口径回答了他关心的问题。

## 14.14 这一层做过的取舍

| 取舍 | 选择 | 代价 | 为什么这么选 |
|---|---|---|---|
| 一个总分 vs 五层分开 | 五层分开，从不跨层聚合 | 没有一个"作品集总分"可以写在简历第一行 | 五层的量纲和分母都不同；平均之后读者无法追问任何一项（§14.2） |
| 评测侧自己定义"合法引用" vs 调用生产函数 | 调用 `collect_reference_sources` | 评测依赖生产模块，生产改了评测会跟着变 | v21 的假阳性证明两份定义一定漂移，而漂移的症状是报告说谎（§14.7.4） |
| 空分母报 0.0 vs 报 1.0 + 暴露分母 | 报 1.0，`treatment_label` 带样本数 | 读者必须看分母才能正确解读 | 安全类指标的分母天然可能为空，报 0.0 等于凭空制造失败（§14.7.7） |
| 修旧口径 vs 并列加新指标 | 并列，旧口径一字不改 | 指标数量增长，读者要理解两个分母 | 修口径会让历史数字失效且不留痕迹（§14.13） |
| 基础设施错误记 0 分 vs 整轮失败 | 整轮失败，异常向上传播 | 真实模型评测要重跑，要再花一次钱 | 记 0 分会让指标同时衡量模型能力和网络质量（§14.10.5） |
| 失败后沿用上次指标 vs 隐藏指标 | `validate_status_payload` 强制隐藏 | 快速模式的报告里有两层是空的 | "上次测出来是 1.0"不是这一轮的结果（§14.9.3） |
| 中文子串断言 vs 结构化字段 | 冲突保护用中文子串 | 文本改由模型生成时判据立刻失效 | 那两句话由确定性解释器生成，测的是解释器而非措辞（§14.5.4） |
| manifest 支持任意 pytest 参数 vs 路径白名单 | 正则白名单 + `shell=False` | 想加一个 pytest flag 要改代码 | 能在数据文件里塞 `--deselect` 的人可以让失败的层报 passed（§14.9.2） |

## 14.15 仍未达标与遗留问题

这一节只列**已经被测量出来、并且写进了仓库文档**的缺口。没测过的东西不在这里，因为"没测"和"测了没达标"是两种不同的状态——把前者写成后者也是一种不诚实。

**真实模型侧（`docs/live-golden-eval-results.md`）：**

1. **P95 ≤ 30 s 未达成。** Run D 三案例平均端到端约 58 s，Run G 因端点故障到 375.3 s / 三案例，Run H 全量均摊约 152.5 s/案例。瓶颈是单次 Planner/Auditor 调用的 10–20 s，不是工具等待。并行工具调用改善不了这一项。
2. **`root_cause_top1_hit_rate` = 0.000（文本相等口径）。** 见 §14.13。锚点口径 Run G 实测 0.500（分母 2）、Run H 实测 0.071（分母 14），与文本相等口径并列发布，彼此不可相减。
3. **`stop_reason_hit_rate` = 0.667。** 跨组件水位案例八步用尽后以 `react_budget_exhausted` 结束，Golden 期望 `evidence_sufficient`。它拿满了必要工具覆盖与证据来源覆盖，属于"证据够了但没在预算内主动收口"——需要在 Prompt 里强化剩余步数临界时的收口判断。这也是 Run G 案例 2 锚点未命中的原因：预算耗尽后报告里根本没有根因。（Run H 的 0.464 分母是 28，其中 11 条没走到收口就 `planner_provider_error`，两者不可直接比较。）
4. **`risk_level_hit_rate` 三案例最后一次实测 0.667、Run H 全量实测 0.500。** 实现约束已在 `graph-seed:v12` 解除（风险等级改由知识节点的 `remediation_risk_level` 声明，`RiskLevel.HIGH` 现在在生产路径上可达），但**两个数字都是各自口径下的最后一次实测值，不得改写或互相替换**；一个案例的实测等级取被召回方案节点的最大值，仍然依赖检索是否选中那个 high 方案。**解除实现约束不等于指标自动达标。**
5. **28 条全量真实模型评测已完成一次，但那一轮由端点超时主导。** Run H（`scope=full`、28/28、142 次调用中 50 次失败）是目前唯一一组全量实测数字，只能读作"链路在全量案例上完整跑通并被评分"，不是模型能力基线（§14.12.1）。第一次 `--all-cases` 尝试在第六条案例上因组件范围推导缺陷中止（已由 `golden-case:v9` 的 `requested_components` 修复），前五条案例的费用已经发生但没有成绩——因为运行器按设计不写半成品 JSON。
6. **Planner 30 s 超时与该模型的响应分布同量级。** Run H 实测成功调用中位 15.3 s、最大 29.9 s，33/86 次 Planner 调用在 30 s 墙上失败。这是当前最大的单一失分来源，但**调整时限之后本轮全部数字作废、必须重测**，预期的改善不能当成成绩。
7. **live 模式的 confirmed 记忆预置机制已经补上，但一次都没跑过，记忆类别四个指标仍不具备发布意义。** Run H 的 `history_recall_coverage` 等四项为 0.000，原因是真实库里没有 confirmed 案例记忆（§14.12.1 第 2 点），不是模型忽略历史。`live-golden-eval:v3` 的 `--seed-history` 用生产 confirm 事务补上了这个前置条件（§14.10.6），但截至本书写作时尚未执行过带预置的真实模型运行；并且预置轮与 Run A–H **不可同列比较**，因为记忆案例的历史根因就是本次正确根因。
8. **`RiskLevel.HIGH` 在真实报告中尚未被观测到一次。** Run H 的 14 条未命中案例实测等级全部是 low。`graph-seed:v12` 解除的是实现上限，不是检索选中率。

**确定性侧：**

9. **28 条案例里只有 14 条有知识图根因锚点。** 剩下 7 条有标注根因但知识图里没有对应的 `root_cause` 节点。要提高锚点覆盖，得去补知识图，不是去调模型（§14.7.5）。
10. **五层的满分全部来自确定性脚本替身。** 这是本章每一节 manifest note 都在重复的限制，也是 CLAUDE.md 明文要求保留的说明。记忆召回那 0.9167 → 1.0 的增益来自六条测试 Provider 生成的角度向量，不是 `bge-m3` 的真实语义空间；Auditor 那三条缺陷是脚本注入的，不是真实模型的错误分布。
11. **`golden_citation_completeness` 只验证引用 ID 存在。** 证据语义是否真的支持结论，这个指标答不了，靠 Auditor 和人工抽查。
12. **两层需要 PostgreSQL。** 没有测试库时只能跑 16/20 个指标，且报告必须标 `complete=false`。

各章还有各自的缺口清单：第 8 章 §8.17、第 9 章 §9.12、第 10 章 §10.13、第 11 章 §11.16、第 12 章 §12.17、第 13 章 §13.11。

## 14.16 小结：全书的最后一条边界

这一章的代码量不算大（五层加运行器约 3500 行），但它是全书唯一一层**输出对象是"结论"而不是"行为"**的代码。前十三章的模块写错了，症状是系统跑不通；这一层写错了，症状是分数变好看。所以它的源码里几乎每一个校验器都在回答同一个问题：**这个数字有资格被发布吗？**

把本章的做法收拢成五条可迁移的规则：

1. **对照组要靠类型保证，不靠纪律。** `vector-only` 出现 graph 通道就崩、`auditor_off` 只能是 `control_unreviewed`、off 组的 `final_report` 必须逐字段等于草稿。"我传了参数所以它应该关了"是意图，"结果里不可能出现那个通道"是证据。
2. **分母必须能从明细复算，并且必须露在报告里。** `validate_case_coverage` 重数案例、类别、锚点适用数；`anchored_case_count`、`measured_scripted_14_anchor_cases` 这些标签不是修饰，是读懂那个比率的必要信息。
3. **"没测"和"测出来是 0"必须是两个词。** `None` 不进分母、`skipped` ≠ `blocked` ≠ `failed`、非 passed 的层一律隐藏指标、`_mean` 空分母的 1.0 是"没有违反项"而不是"能力完美"。
4. **任何"什么算合法"的判定，生产代码里已有权威定义时，评测不能有第二份。** v21 的引用假阳性和 Run C 的白名单退步是同一个病的两次发作，两次的症状都是"报告说系统更差"。
5. **口径有问题时，加新指标并列发布，不改旧指标。** 旧数字保留、两个分母同时可见、真要改口径就显式作废历史。

以及这一章反复出现、也最容易在求职场景里被违反的一条：**清楚地说出一个数字不能证明什么。** 五层的满分只证明确定性数据集完整和打分逻辑被执行过；`history_impact` 的 Δ0.3333 只证明历史上下文进入了真实 LangGraph，没有证明准确率提升；Run F 的 1.000 只证明加了重试后链路正常，没有证明重试救回过调用。这些"只证明……不证明……"的句子写起来毫无成就感，但它们是这份报告里唯一不会被推翻的部分。

回头看全书的十四章，它们其实在讲同一件事的十四个侧面：**先把不可协商的东西钉成类型，再让所有行为从类型里长出来。**

第 1 章把工具名、证据、假设、报告钉成契约；第 2 章让契约 ID 不一致时进程根本起不来；第 3 章把 MCP 做成真的进程边界，禁止在 Agent 节点里读 Fixture 冒充工具调用；第 4 章把"能力"降级成配置，因为它一旦能调模型就会变成第三个 Agent；第 5、6 章让检索的每一条结果都带着可引用的稳定 ID；第 7 章把模型输出关进 Structured Outputs，并把重试和遥测做成边界内的事；第 8 章给 ReAct 加上步数预算，并让所有门禁整批拒绝而不截断；第 9 章让确定性规则对模型有非对称否决权，并区分"审计通过"和"审计不可用"；第 10 章让实时 Observation 永远优先于历史，并把记忆入库放在审计之后；第 11 章让 run 由 Worker 执行、失败不覆盖旧快照；第 12 章让推理过程在结构上不可能泄漏、鉴权 fail-closed；第 13 章让部署的每一步都有一个可验证的出口。

到了第 14 章，同一个思路指向了最后一个对象——**报告里的数字本身**。它也需要契约、也需要门禁、也需要"结构上不可能作假"，因为它是这些代码唯一会被别人直接引用的产物。

一个系统的诚实程度，不体现在它宣称的分数有多高，而体现在它**有多少条路径可以让分数变得不诚实，以及这些路径是否都被封死了**。这份文档从头到尾在读的，就是那些封路的代码。



