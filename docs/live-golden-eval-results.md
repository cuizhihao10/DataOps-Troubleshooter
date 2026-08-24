# 真实模型 Golden 评测状态与运行口径

## 当前状态

`live-golden-eval:v1` 已经在固定第三方 OpenAI 兼容端点的 `gpt-5.6-sol` 上执行，本文
**只发布三案例 smoke 的实测成绩**（`metric_kind=measured`、`scope=smoke`、
`case_coverage_rate=0.107`）。28 条完整 Golden 集合、真实生产故障分布和多模型对比都尚未测量，因此
本文所有数字只能读作"这套链路在这三条案例上确实跑通并被评分"，不能读作模型质量结论。

仓库仍然不提交任何密钥、端点地址或原始报告 JSON：`live-golden*.json` 已在 `.gitignore` 中，对外
口径只保留本文的聚合数字与逐案例判定。

## v1 默认冒烟集合

默认集合固定三条案例，并保持以下顺序：

1. `golden_lts_invalid_partition_parameter_single`：单组件，要求日志正证与上游就绪反证同时存在。
2. `golden_cross_lts_bds_flashsync_watermark_timezone_mismatch`：三组件传播，要求 900 条缺口闭合。
3. `golden_bds_conflicting_partition_evidence`：三个工具均成功但事实冲突，要求无根因并人工复核。

这个三案例集合只用于低成本接线和安全冒烟，不代表 28 条完整真实模型成绩。显式传多个
`--case-id` 会生成 `scope=custom`，不能冒充标准 smoke；未来发布完整 28 条测量快照时应升级单独的
运行/结果契约，并记录所有类别分母。

## 三次实测对比（全部为实测值）

固定条件：同一份 `golden-case:v7` 三案例、同一 `bge-m3:v1` 向量空间、同一 `auditor-report:v2`、同一
`gpt-5.6-sol`；唯一变量是 Planner Prompt 版本与报告返工实现。

| 指标 | Run B `planner-react:v6` | Run C `planner-react:v7` | Run D `planner-react:v8` + 定位修订 |
|---|---|---|---|
| `intent_accuracy` | 1.000 | 1.000 | 1.000 |
| `necessary_action_coverage` | 0.944 | 0.778 | **1.000** |
| `evidence_source_coverage` | 0.944 | 0.778 | **1.000** |
| `fault_path_completeness` | 0.000 | 0.167 | **0.667** |
| `stop_reason_hit_rate` | 0.000 | 0.667 | 0.667 |
| `risk_level_hit_rate` | 0.333 | 0.333 | **0.667** |
| `accepted_report_rate` | 0.333 | 0.667 | 0.667 |
| `root_cause_top1_hit_rate` | 0.000 | 0.000 | 0.000 |
| `citation_completeness` | 1.000 | 1.000 | 1.000（v22 口径） |
| `unsupported_critical_claim_rate` | 0.000 | 0.000 | 0.000（v22 口径） |
| `duplicate_action_rate` | 0.000 | 0.000 | 0.000 |
| `tool_attempt_success_rate` | 0.917 | 0.818 | 0.857 |
| `safe_degradation_rate` | 1.000 | 1.000 | 1.000 |
| `evidence_conflict_safe_resolution_rate` | 1.000 | 1.000 | 1.000 |
| `history_*` 五项与 `forbidden_*` 两项 | 全部满足 | 全部满足 | 全部满足 |
| 模型调用次数 | 15 | 15 | **12** |
| 总 token | 135,685 | 151,199 | **126,670** |
| 三案例总耗时 | 275.6 s | 209.7 s | **175.2 s** |

两个引用指标标注"v22 口径"是因为它们来自**同一份 Run D 持久化运行结果的离线重评分**，不是新的模型
调用：Run D 原始报告在 `golden-diagnosis-eval:v21` 下读到 `citation_completeness=0.875`、
`unsupported_critical_claim_rate=0.125`，经核对是评分器缺陷（详见下一节）。修正后的 v22 评分器直接读
PostgreSQL 里 Run D 的 `agent_runs.result` 重新打分，其余十二项指标逐项复现 v21 的实测值（意图 1.000、
必要 Action 1.000、Evidence source 1.000、链路 0.667、停止原因 0.667、风险 0.667、接受率 0.667、Top-1
0.000、重复率 0.000、工具成功率 0.857、安全降级 1.000、冲突安全解决 1.000），因此这两项的变化只能来自
判定口径而不是报告内容。Run B/C 的 1.000/0.000 不需要重评：v21 要求"所有引用都在支撑集合内"，v22 只
要求"至少一条在支撑集合内"，前者成立必然蕴含后者成立。

Run D 的模型调用延迟（实测值，单调时钟）：Planner 七次调用 10.3–15.9 s（中位 12.6 s，最大输出 788
token），Auditor 五次调用 7.2–18.7 s（中位 16.3 s，最大输出 829 token），全部 `succeeded`，
`output_invalid_call_count=0`、`unreported_usage_call_count=0`。三案例平均端到端约 58 s，**明显超出
产品设计的 P95 ≤ 30 s 目标**；该目标是在同等硬件与更快模型端点下的设计值，当前第三方端点的单次
Planner/Auditor 延迟就已经占满预算，因此不能宣称已达成。并行工具调用只压缩了 MCP 等待时间，模型
串行思考时间仍是主要成本。

## Run D 逐案例判定（实测值）

| 案例 | 执行工具数 | 缺失必要工具 | 停止原因 | 报告终态 | 风险等级 |
|---|---|---|---|---|---|
| `golden_lts_invalid_partition_parameter_single` | 3 | 无 | `evidence_sufficient`（命中） | `accepted`（返工 1 次后） | medium（命中） |
| `golden_cross_lts_bds_flashsync_watermark_timezone_mismatch` | 8 | 无 | `react_budget_exhausted`（未命中） | `accepted`（返工 0 次） | low（期望 high，未命中） |
| `golden_bds_conflicting_partition_evidence` | 3 | 无 | `evidence_conflict_requires_manual_review`（命中） | `degraded` 且公开不确定性 | low（命中） |

第三条案例的 `report_accepted=False` 是**期望行为**而不是失败：该案例三个工具都成功但事实互相冲突，
Golden 要求系统不给根因、公开冲突并要求人工复核，`safe_degradation_hit=True`、
`evidence_conflict_safe_resolution=True` 都已命中，`forbidden_conflict_root_hit_count=0`。因此
`accepted_report_rate=0.667` 在这个三案例集合上就是上限，不应被当作三分之一报告不合格。

## Run D 相对 Run C 的两项结构性修复

1. **可引用 ID 白名单与报告层同源。** v7 的 Planner 白名单比 `collect_reference_sources` 更窄，模型
   引用 Prompt 里刚给出的 `kn_*` 知识证据反而被控制器整批拒绝；v8 把两侧统一，并在 Prompt 中标注
   每个 ID 的 `source`。`necessary_action_coverage` 与 `evidence_source_coverage` 从 0.778 回到 1.000，
   `fault_path_completeness` 从 0.167 升到 0.667。
2. **返工改为按 `claim_path` 定位删除。** Run C 的案例 1 状态里已有两条 `supported` 假设（含 Golden
   根因），但首轮审计一旦触发返工就整类清空根因与链路，第二轮 Auditor 又对修订稿自己写下的
   "证据不足"表述提出 `evidence_conflict`，一次返工预算耗尽后只能 `safe_degraded`。Run D 同一案例
   在返工 1 次后 `accepted`，并保留了带引用的根因与完整传播链路（`fault_path_completeness=1.0`）。

## 仍未达标与已知测量口径缺陷

- `root_cause_top1_hit_rate` 实测仍为 0.000。原因不是模型没给出根因：Run D 案例 1 输出的根因文本
  已经准确说明 `partition_date` 用了 `20260713` 而任务声明要求 `yyyy-MM-dd`。评分器用**精确字符串
  相等**把报告根因与知识图节点名比较，一段自然语言句子永远不可能相等。要让这个指标可达，必须先给
  Golden 案例引入规范化根因锚点（节点 ID 或受控标签），否则它衡量的是措辞而不是正确性。
- `risk_level_hit_rate` 只到 0.667，缺口来自跨组件案例期望 high 而实测 low。确定性
  `_build_remediation_steps` 目前不可能产出 `RiskLevel.HIGH`，因此该指标的上限受实现约束而非模型
  约束；需要让知识库声明方案的风险等级后才能真正测量。
- `citation_completeness` 与 `unsupported_critical_claim_rate` 在 v21 下读到的 0.875 / 0.125 是
  **评分口径缺陷而非报告质量下降**，现已修复为 `golden-diagnosis-eval:v22`。Run D 案例 1 的根因引用集合
  为三条实时 Observation + 一条 `kn_*` 知识节点 + 一条 `path_*`，而 v21 的 `valid_refs` 只收 `state.evidence`、候选
  path 和召回记忆，不收 Bundle 知识节点；判定又是"所有引用都必须在集合内"的单一 AND 条件，于是多引用
  一条合法知识依据反而被记为悬空引用。v22 把它拆成两条独立规则：悬空判定复用报告层
  `collect_reference_sources`（因此 Bundle 知识节点与文档切片同样合法），实时支撑判定要求每条关键结论
  至少有一条引用落在本次 Observation、可引用图路径或已确认案例上——只放宽前者会让模型复述知识库就能
  拿满引用分。升版同时提升了 `portfolio-eval-manifest:v23`，五层确定性评测已重跑（见
  `docs/portfolio-eval-results.md`），Golden 层 28/28 不变。
- `stop_reason_hit_rate` 卡在 0.667：跨组件案例八步用尽后以 `react_budget_exhausted` 结束，
  Golden 期望 `evidence_sufficient`。它拿满了必要工具覆盖与证据来源覆盖，属于"证据够了但没在预算内
  主动收口"，需要在 Prompt 里进一步强化剩余步数临界时的收口判断。

## 真实执行路径

命令在同一进程进入 FastAPI lifespan，启动并审计生产依赖，然后为每条案例创建独立 PostgreSQL
session：

```text
load golden-case:v7
  -> validate local settings and select case IDs
  -> FastAPI lifespan validates Fixture / Prompt / Graph / real MCP discovery
  -> PostgreSQL GraphRAG retrieves an Evidence Bundle
  -> Planner Structured Outputs chooses Action
  -> LangGraph executes the Action through stdio MCP
  -> Observation returns to the bounded Planner loop
  -> deterministic report policy + independent Auditor
  -> audited memory staging and persisted run/events/checkpoint
  -> golden-diagnosis-eval:v22 scores the public DiagnosisRunResult
  -> live-golden-eval:v1 aggregates safe model-call telemetry
```

Live runner 不调用确定性 Golden runner，也不读取 Fixture 响应拼装答案。它只给 Planner 追加合成
`scenario_id`、资源 ID 和观察窗口，这些是 Mock MCP 的寻址字段；`required_tools`、允许根因、必要
Evidence source、故障路径、预期停止原因和风险答案不会进入模型消息。测试逐项断言这些标注没有泄漏。

一个必须先满足的前置条件：`-m postgres` 集成测试会把知识节点按确定性 hash 向量重写，跑完测试后
必须重新执行 `python -m app.persistence.seed`，否则 lifespan 会以
`all knowledge nodes must be embedded in the configured provider space` 拒绝启动。这不是缺陷，而是
"全有或全无"的向量空间一致性检查在起作用。

## 安全遥测原理

Planner 和 Auditor Provider 在每次 `complete` 前创建 `ModelCallMeasurement`。只有 CLI 用
`ContextVar` 绑定 `InMemoryModelCallRecorder` 时才记录；普通 API 请求没有绑定，因此 Provider 不保存
并发不安全的 `last_usage`，也不会在生产进程中无限积累调用对象。异步 task 继承自己的上下文，评测
结束后在 `finally` 恢复旧 token，避免后续请求写入已结束的报告。

`model-call-metric:v1` 只允许以下字段：角色、Provider 契约、模型名、Prompt 契约、稳定状态、单调
时钟耗时，以及供应商可选的 input/output/total token。Schema 没有消息、Prompt、响应、base URL、
凭据或 Thought 字段。兼容端点不返回 usage 时，调用计入 `unreported_usage_call_count`，不会伪造零成本。

## 本地运行

先确保 PostgreSQL 已迁移并载入当前知识种子，然后只在本地进程设置密钥：

```powershell
$env:DATAOPS_DATABASE_URL='postgresql+asyncpg://dataops:本地密码@127.0.0.1:15432/dataops'
$env:DATAOPS_CHAT_PROVIDER='openai-compatible'
$env:DATAOPS_CHAT_MODEL='固定模型名称'
$env:DATAOPS_CHAT_BASE_URL='https://兼容端点/v1'
$env:DATAOPS_CHAT_API_KEY='本地密钥，不写入文件'
.venv\Scripts\python -m app.evaluation.live_golden `
  --code-revision '<git commit>' `
  --output 'live-golden-smoke.json'
```

运行失败不写半成品 JSON。成功报告会同时保存代码版本、模型/Prompt/Embedding/Golden 契约、案例
明细、Golden 指标、调用次数、结构失败数、usage 缺失数、token 和耗时。生成的本地报告在公开前还
应人工检查模型名和错误分类；任何真实密钥、内部 URL 或生产数据都不得加入仓库。

## 当前不能宣称什么

- 不能把三案例 smoke 外推到 28 条或生产故障分布；`target_coverage_complete=false` 必须一起给出。
- 不能宣称达成 P95 ≤ 30 s：实测三案例平均端到端约 58 s，该目标仍是设计目标值。
- 不能把 `root_cause_top1_hit_rate=0` 说成"模型找不到根因"，也不能把它悄悄改口径后当成提升。
- 不能把 MockTransport 的固定 15 token 响应当作模型成本实测。
- 不能把确定性 Golden runner 的 28/28 满分当作 Planner/Auditor 质量。
- 不能因为 token/耗时可观测就保存 Prompt、模型原始响应或 Thought。


