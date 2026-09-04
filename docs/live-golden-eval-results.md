# 真实模型 Golden 评测状态与运行口径

## 当前状态

`live-golden-eval:v3` 已经在固定第三方 OpenAI 兼容端点的 `gpt-5.6-sol` 上执行。本文的 Run A–G
**只发布三案例 smoke 的实测成绩**（`metric_kind=measured`、`scope=smoke`、`case_coverage_rate=0.107`）；
Run H 是第一组 `scope=full` 实测数字（28/28、`case_coverage_rate=1.000`），但那一轮 142 次调用里有 50 次
失败（44 次超时），**成绩主要由端点状况与超时配置决定，不能读作模型质量结论**。真实生产故障分布与
多模型对比仍未测量。

下表 Run A–G 是在 `live-golden-eval:v1` 契约下产生的。v1 到 v2 只把 `scope` 枚举从两档扩成三档
（新增 `full`），没有改动案例选择、评分器、Prompt 或分母，因此这些行的口径与数值原样有效，不需要
也不允许因为契约升版而改写。v2 到 v3 同样不改评分器与 Prompt，只新增 opt-in 的 `--seed-history`
历史预置与报告字段 `history_seed`。

**Run A–H 全部在没有历史预置的条件下运行**，因此四个记忆类指标（`history_recall_coverage`、
`confirmed_only_recall_rate`、`history_projection_pass_rate`、`realtime_priority_rate`）在这些轮次里
一律为 0，含义是"数据库里没有 confirmed 历史案例，这四项没测"，**不是"模型没做到"**。v3 的
`--seed-history` 提供了这个前置条件，Run I 是第一轮带预置的实测运行：四个指标终于拥有真实非空分母，
其中两项实测 1.0000、两项实测 0.0000，而后两项的 0 由**报告被安全降级**而不是历史处理错误造成（详见
"Run I"一节）。Run I 只覆盖 3 条记忆案例（`scope=custom`），与 Run A–H 不可同列比较。

仓库仍然不提交任何密钥、端点地址或原始报告 JSON：`live-golden*.json` 已在 `.gitignore` 中，对外
口径只保留本文的聚合数字与逐案例判定。

**Prompt 已升到 `planner-react:v9`、循环契约已升到 `langgraph-react-loop:v4`（收口回合），而本文
Run A–I 全部在 `planner-react:v8` + `langgraph-react-loop:v3` 下测得。** v9/v4 尚未发布任何真实模型
实测运行，因此下面所有数字都不能当作 v9 的成绩读，也不能因为"结构上修掉了一条失败面"就预期任何
指标上升。这次改动的可验证范围只有单元与集成测试：预算打满后 Planner 确实拿到一次只允许 finish 的
回合、该回合的假设更新确实进入状态、额度确实只发一次。它对 `stop_reason_hit_rate` 与
`root_cause_top1_hit_rate` 的影响是**待测量项**，需要在同一端点上重跑一轮才能发布。

最近一次真实模型运行是 Run I（`scope=custom`，3 条记忆案例，第一轮带 `--seed-history` 预置，见
"Run I"一节）；最近一次全量运行仍是 Run H（`scope=full`，28 条）。在它们之前的 Run G（评分器
`golden-diagnosis-eval:v23`）给出了新增指标 `root_cause_anchor_hit_rate` 的第一个实测值 0.500，分母
`anchored_case_count=2`；同一轮里 `root_cause_top1_hit_rate` 仍然是 0.000。这两个数字是**分母与口径都
不同的两个独立指标**，必须并列阅读，不能相减、不能互相替换（详见"Run G"与"仍未达标"两节）。Run G
中途遇到端点不可用，多数指标低于 Run D/F，这一点也在对应小节里如实标注。

## 默认冒烟集合与三档样本口径

默认集合固定三条案例，并保持以下顺序：

1. `golden_lts_invalid_partition_parameter_single`：单组件，要求日志正证与上游就绪反证同时存在。
2. `golden_cross_lts_bds_flashsync_watermark_timezone_mismatch`：三组件传播，要求 900 条缺口闭合。
3. `golden_bds_conflicting_partition_evidence`：三个工具均成功但事实冲突，要求无根因并人工复核。

这个三案例集合只用于低成本接线和安全冒烟，不代表 28 条完整真实模型成绩。`scope` 分三档且判定规则
不对称：与上述序列逐个相同才是 `smoke`（保证多轮逐案可比），覆盖 Golden 全部 28 条才是 `full`
（用集合比较，与执行顺序无关），其余显式子集一律 `custom`——少一条立即退回 `custom`，"接近全集"
不能被读成全量。全量快照由 `--all-cases` 显式请求，它与 `--case-id` 互斥并在产生模型费用前失败。

## 历史预置与记忆类分母（`--seed-history`）

确定性 Golden runner 由脚本替身直接合成 `CaseMemoryMatch`，而 live 模式走的是生产 confirmed-only
检索路径。数据库里没有 confirmed 案例时，`history_recall_coverage`、`confirmed_only_recall_rate`、
`history_projection_pass_rate`、`realtime_priority_rate` 必然全部为 0——这是**分母为空**，与模型行为
无关。Run A–H 都处于这个状态。

`live-golden-eval:v3` 新增 opt-in 的 `--seed-history`，在任何付费聊天调用之前把 Golden 的
`history_expectation` 标注投影成真实数据库行：required 记忆经生产 `decide(CONFIRM)` 事务变成
confirmed（因此动态 case 图节点与 `SIMILAR_TO` 边与用户在 `/demo` 点击确认时完全同源），forbidden
记忆按声明顺序分别落在 pending 与 rejected，用来真正执行状态过滤而不是靠"向量刚好不相似"侥幸通过。
顺序固定为"按 ID 删除旧行 → 单事务批量插入 pending → 逐条走生产决策"，任何一步失败都整批抛错终止
评测，绝不降级成一轮记忆指标为 0 的付费运行。

三条必须一起阅读的口径约束：

1. 预置内容只来自案例的用户问题与 `history_expectation`，`allowed_root_causes`、`required_tools`、
   必要 Evidence source、故障路径与预期停止原因都不进入任何字段。
2. 但 `historical_root_cause` 本身是标注的一部分，非冲突案例的历史根因就等于本次正确根因。因此
   **开了预置的运行与 Run A–H 不可同列比较**：记忆类案例的根因指标分母口径已经变了。
3. 报告里的 `history_seed` 字段公开本轮实际写入了哪些 confirmed / pending / rejected ID 以及向量
   空间（Provider ID 与维度）；缺少该字段或为 `null` 就表示这一轮没有历史分母。

默认关闭是有意的：预置会真实写库，默认打开等于替使用者决定数据库内容，之后没人能从报告分辨某条
confirmed 案例是评测放进去的还是用户确认的。第一次真实预置在付费调用之前就失败了——
`app/memory/graph_registration.py` 的 `case_graph_node_id` 抛出 `case graph memory_id must use
mem_<16 hex> format`，因为 Golden 标注写的是生产永远铸造不出来的可读 ID。修法是收紧数据集到
`golden-case:v10`（形状 `mem_` + 16 位小写 hex，required 与 forbidden 共用同一别名），而不是放宽生产
约束；这个缺陷在离线回归里完全不可见，因为确定性替身的 `RecordingMemoryRuntime.decide` 不校验 ID
形状。修复后的第一轮预置运行即 Run I，机制到那一刻才从"已实现"变成"已测量"。

## 三次实测对比（全部为实测值）

固定条件：同一份 `golden-case:v8` 三案例、同一 `bge-m3:v1` 向量空间、同一 `auditor-report:v2`、同一
`gpt-5.6-sol`；唯一变量是 Planner Prompt 版本与报告返工实现。

| 指标 | Run B `planner-react:v6` | Run C `planner-react:v7` | Run D `planner-react:v8` + 定位修订 | Run F `model-transient-retry:v1` |
|---|---|---|---|---|
| `intent_accuracy` | 1.000 | 1.000 | 1.000 | 1.000 |
| `necessary_action_coverage` | 0.944 | 0.778 | **1.000** | 0.833 |
| `evidence_source_coverage` | 0.944 | 0.778 | **1.000** | 0.833 |
| `fault_path_completeness` | 0.000 | 0.167 | **0.667** | 0.667 |
| `stop_reason_hit_rate` | 0.000 | 0.667 | 0.667 | 0.667 |
| `risk_level_hit_rate` | 0.333 | 0.333 | **0.667** | 0.667 |
| `accepted_report_rate` | 0.333 | 0.667 | 0.667 | 0.667 |
| `root_cause_top1_hit_rate` | 0.000 | 0.000 | 0.000 | 0.000 |
| `citation_completeness` | 1.000 | 1.000 | 1.000（v22 口径） | 1.000（v22 原生） |
| `unsupported_critical_claim_rate` | 0.000 | 0.000 | 0.000（v22 口径） | 0.000（v22 原生） |
| `duplicate_action_rate` | 0.000 | 0.000 | 0.000 | 0.000 |
| `tool_attempt_success_rate` | 0.917 | 0.818 | 0.857 | **1.000** |
| `safe_degradation_rate` | 1.000 | 1.000 | 1.000 | 1.000 |
| `evidence_conflict_safe_resolution_rate` | 1.000 | 1.000 | 1.000 | 1.000 |
| `history_*` 五项与 `forbidden_*` 两项 | 全部满足 | 全部满足 | 全部满足 | 全部满足 |
| 模型调用次数 | 15 | 15 | **12** | 13 |
| 总 token | 135,685 | 151,199 | **126,670** | 189,996 |
| 三案例总耗时 | 275.6 s | 209.7 s | **175.2 s** | 227.2 s |

Run F 的两项指标标注"v22 原生"是因为它们**来自新的模型调用直接评分**，不是离线重评分：这是
`golden-diagnosis-eval:v22` 拆分悬空/实时支撑两条规则之后的第一次真实模型运行，两项引用指标在真实
报告上原生复现了 1.000 / 0.000。

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

## Run E 与 Run F：端点限流暴露的重试缺口

Run E 与 Run F 之间只有一处代码变化：`model-transient-retry:v1`（实现见实现指南第 23 节）。

**Run E 不发布聚合成绩**，因为它测到的是端点配额而不是模型质量：同一端点在约 92 s 内成功完成 6 次
调用（单次 8.5–22.9 s），随后连续 4 次返回 HTTP 错误，每次只用 0.26–0.63 s 就被驳回——响应时间差两个
数量级，说明后 4 次根本没打到模型。两个案例因此以 `planner_provider_error` 终结，其中一个连第一个
工具都没执行，`necessary_action_coverage` 直接归零。把 0.667 / 0.333 这类数字作为模型成绩发布会违反
本仓库的评测诚实性要求，因此只保留这段成因记录。触发原因也如实记录：Run E 与前一次因运行器缺陷
崩溃的运行相隔不到 4 分钟，几乎肯定是短时间内两轮完整评测打满了配额窗口。

Run F 是加上重试后的干净运行（实测值，单调时钟）：13 次调用**全部 `succeeded`**，
`output_invalid_call_count=0`、`unreported_usage_call_count=0`、`tool_attempt_success_rate=1.000`。
Planner 八次调用 5.9–23.1 s，Auditor 五次调用 3.5–33.8 s。

必须诚实说明的一点：**Run F 没有触发任何重试**。本次运行没有出现瞬时失败，因此这份报告只能证明
"加了重试之后链路仍然正常"，**不能作为"重试在生产端点上成功救回了调用"的证据**。重试路径的行为
由单元测试用注入 sleep 与脚本化 Provider 覆盖（退避序列、认证失败不重试、预算耗尽原样上抛），真实
端点上的救回效果需要下一次恰好遇到 429 的运行才能测量。

## 被丢弃的 `gpt-5.6-terra` 探测运行：退避在真实端点上执行过，但没救回任何调用

同一端点还提供 `gpt-5.6-terra`，单次 trivial Structured Outputs 调用只要 6.3 s（`gpt-5.6-sol` 同类
调用 18.0 s），因此值得试一次能否压低端到端延迟。**这次运行的聚合指标一律不发布**，理由与 Run E
相同：三案例全部以 `planner_provider_error` 收口、执行工具数为 0，测到的是网关容量而不是模型质量。

它仍然贡献了一条 Run F 拿不到的实测事实：**12 条模型调用记录恰好是 3 案例 ×（Planner 2 次 + Auditor
2 次）**，即每个逻辑调用都真的按 `max_attempts=2` 重发了一次，且两次尝试各自落成独立的
`model-call-metric:v1` 记录（10 次 `http_error`、2 次 `connection_error`，单次 1.5–12.3 s）。这证明
退避包装层在真实端点上会执行、并且保持了"一次尝试一条可归因记录"的遥测粒度；但两次尝试都失败，
所以**"重试成功救回调用"依旧没有真实端点证据**。

失败原因经单独探测确认为网关侧资源未分配，而不是 Schema 被拒：terra 在最小 `{ok: bool}` Schema 与
完整 `PlannerDecision` strict Schema 下都返回 `HTTP 500 / E41001 Waiting for service resources to be
allocated`，同一时刻同一密钥的 `gpt-5.6-sol` 调用正常返回。因此结论是 terra 在本工作负载下不可用，
固定模型继续保持 `gpt-5.6-sol`；这也说明它那次 6.3 s 的快只是偶然命中了已分配实例。

Run F 的三案例总耗时 227.2 s（约 75.7 s/案例），比 Run D 的 175.2 s（约 58 s/案例）更慢，token 也从
126,670 升到 189,996。这**不是重试造成的**（没有重试发生），而是端点单次延迟的自然波动加上本轮
Auditor 一次 33.8 s 的长调用；同时也再次说明 P95 ≤ 30 s 仍未达成。`necessary_action_coverage` 从
Run D 的 1.000 回落到 0.833，缺口全部来自跨组件案例只执行了 3 个工具中的一半（缺
`lts.get_dependency_topology`、`bds.get_table_info`、`flashsync.get_sync_log`），并以
`invalid_evidence_reference` 收口——这属于同一 Prompt 下的运行间波动，三案例样本无法区分它与真实回退，
不应被解读为 `planner-react:v8` 退化。

## Run F 逐案例判定（实测值）

| 案例 | 执行工具数 | 缺失必要工具 | 停止原因 | 风险等级 |
|---|---|---|---|---|
| `golden_lts_invalid_partition_parameter_single` | 3 | 无 | `evidence_sufficient`（命中） | 命中 |
| `golden_cross_lts_bds_flashsync_watermark_timezone_mismatch` | 3 | 3 个 | `invalid_evidence_reference`（未命中） | 未命中 |
| `golden_bds_conflicting_partition_evidence` | 3 | 无 | `evidence_conflict_requires_manual_review`（命中） | 命中 |

## Run G：`golden-diagnosis-eval:v23` 下的第一次真实模型运行（实测值）

Run G 的目的只有一个：在真实模型报告上测量新增的 `root_cause_anchor_hit_rate`。它**不是离线重评分**。
原计划是复用 Run D/F 持久化的 `agent_runs.result` 重新打分，但那些行已被后续 `-m postgres` 集成测试与
五层评测的建表/清表流程清空，因此拿到锚点实测值必须重新调用模型。命令与前几轮完全一致，只有评分器
从 v22 升到 v23（新增一个指标，既有指标定义一字未改）。

| 指标 | Run D `planner-react:v8` | Run F `model-transient-retry:v1` | Run G `golden-diagnosis-eval:v23` |
|---|---|---|---|
| `root_cause_top1_hit_rate` | 0.000 | 0.000 | 0.000 |
| `root_cause_anchor_hit_rate` | 未测量（v22 无此指标） | 未测量（v22 无此指标） | **0.500** |
| `anchored_case_count` | — | — | 2 |
| `intent_accuracy` | 1.000 | 1.000 | 1.000 |
| `necessary_action_coverage` | 1.000 | 0.833 | 0.667 |
| `evidence_source_coverage` | 1.000 | 0.833 | 0.667 |
| `fault_path_completeness` | 0.667 | 0.667 | 0.500 |
| `stop_reason_hit_rate` | 0.667 | 0.667 | 0.333 |
| `risk_level_hit_rate` | 0.667 | 0.667 | 0.667 |
| `citation_completeness` | 1.000 | 1.000 | 1.000 |
| `unsupported_critical_claim_rate` | 0.000 | 0.000 | 0.000 |
| `duplicate_action_rate` | 0.000 | 0.000 | 0.000 |
| `tool_attempt_success_rate` | 0.857 | 1.000 | 0.818 |
| `safe_degradation_rate` | 1.000 | 1.000 | 1.000 |
| `evidence_conflict_safe_resolution_rate` | 1.000 | 1.000 | 0.000 |
| `accepted_report_rate` | 0.667 | 0.667 | 0.333 |
| 模型调用次数 | 12 | 13 | 14（8 成功、6 失败） |
| 总 token | 126,670 | 189,996 | 88,819 |
| 三案例总耗时 | 175.2 s | 227.2 s | 375.3 s |

**Run G 的多数指标低于 Run D/F，原因是端点在这一轮中途不可用，不是 Prompt 或代码退化。** 14 次调用里
6 次以 `timeout` 或 `connection_error` 结束，构成三组"首次调用 + 一次重试"，每组都在重试后仍失败：
Auditor 两次 90.0 s 超时（等于配置的 Auditor 墙钟预算）、两次 5.0/16.2 s 连接错误，Planner 一次 30.0 s
超时加一次 16.4 s 连接错误。`output_invalid_call_count=0`，说明没有任何一次是结构化输出不合法；
`unreported_usage_call_count=6` 与失败次数一致，失败调用本来就没有 usage 可记。这同时是
`model-transient-retry:v1` 的完整实测证据：有界重试确实在真实端点上执行了，上限确实是一次，并且
在端点持续不可用时不会退化成无限重试。

### Run G 逐案例判定（实测值）

| 案例 | 执行工具数 | 缺失必要工具 | 停止原因 | 锚点判定 |
|---|---|---|---|---|
| `golden_lts_invalid_partition_parameter_single` | 3 | 无 | `evidence_sufficient`（命中） | **命中** `root_cause_lts_invalid_partition_parameter` |
| `golden_cross_lts_bds_flashsync_watermark_timezone_mismatch` | 8 | 无 | `react_budget_exhausted`（未命中） | 未命中（报告没有任何根因） |
| `golden_bds_conflicting_partition_evidence` | 0 | 3 个 | `planner_provider_error`（未命中） | 不适用（案例无允许根因，不进分母） |

案例 1 是这次新增指标的关键一行：**`root_cause_top1_hit=false` 与 `root_cause_anchor_hit=true` 同时成立。**
模型输出的根因文本准确说明 `partition_date` 用了 `20260713` 而任务要求 `yyyy-MM-dd`，但它是一整句自然
语言，与知识节点名不可能字符串相等；而它的 Top-1 根因引用集合里包含
`kn_root_cause_lts_invalid_partition_parameter`，并且该条结论的全部引用非悬空、至少一条落在本轮
Observation 上，因此锚点判定成立。这正是两个口径的差别在真实报告上的第一次实测体现。

案例 2 的锚点未命中不是"指向了错误的故障模式"：该案例三条并行批次共 8 个 Action 用满步数预算，
`react_budget_exhausted` 之后报告里根本没有根因，`cited_root_cause_anchors` 为空。它对应的是已记录的
收口缺口（见下节），不是检索错节点。案例 2 这一轮的必要工具覆盖是满的（Run F 曾缺 3 个），说明并行
取证确实换来了更多证据，但也更快耗尽步数预算。

案例 3 的 0 工具与 `planner_provider_error` 来自端点不可用，因此这一轮
`evidence_conflict_safe_resolution_rate` 读到 0.000。为了确认这是端点事件而不是冲突处置退化，我用
`--case-id golden_bds_conflicting_partition_evidence` 单独重跑（Run G2，`scope=custom`，5 次调用、
69,535 token、57.2 s）：停止原因回到 `evidence_conflict_requires_manual_review`，三个矛盾 source 全部被
观察，禁止根因零命中，uncertainty 已公开，`evidence_conflict_safe_resolution=true`、报告 accepted。
**Run G2 不能并入 Run G 的 smoke 数字**——它的 scope 不同、分母只有 1，只能作为"同一案例在端点恢复后
可复现安全处置"的旁证。

Run G2 还顺带暴露了一个必须一起读的口径约定：它的 `root_cause_anchor_hit_rate` 显示 1.000 而
`anchored_case_count=0`。空分母按 1.000 上报（`root_cause_top1_hit_rate` 与 `fault_path_completeness`
在该运行里同理），这是"本轮没有可判定案例"而不是满分。看锚点比率必须同时看 `anchored_case_count`，
报告层的不变量会强制这个分母从案例明细复算，正是为了让这种误读在数据里就能被发现。

## 被中止的首次全量运行：`--all-cases` 暴露的组件范围契约缺陷

`live-golden-eval:v2` 加上 `--all-cases` 之后的第一次全量尝试**没有产生任何成绩**，本节只记录它的
失败原因，因为它测到的是评测入口自己的数据契约缺陷。

失败点在第六条案例 `golden_lts_upstream_not_ready_single` 构造生产消息时：18 个 Fixture 被 28 条案例
复用，这条单组件案例与两条跨组件案例共用三组件场景 `cross_chain_pk_conflict`，而旧实现把
`DiagnosisMessage.components` 从 Fixture 的组件列表推导，于是 `single_component_diagnosis` 意图拿到三个
组件，被 `CapabilitySelectionRequest` 的元数校验整条拒绝（`single-component diagnosis requires exactly
one component`）。六条案例（第 6–11 条，含三条记忆案例）都属于这个形状。

必须如实说明代价：案例按声明顺序串行执行，因此前五条案例的真实模型调用已经发生并产生费用，而
`run_live_golden_evaluation` 按设计在失败时不写半成品 JSON，所以这些调用既没有成绩也没有可发布的
token/耗时明细——本文无法给出它的任何数字，只能给出失败原因。

修复方式是把组件范围变成显式输入而不是推导结果：`golden-case:v9` 新增 `requested_components`，加载
阶段强制它与 `expected_intent` 的元数一致、并覆盖全部 `required_tools` 所属组件；确定性 runner 与真实
模型 runner 都读同一个字段。升版只加输入字段，不动评分器、Prompt 与分母，因此上文 Run A–G 的数值
继续有效。同时补了一条零成本单测逐条构造 28 个生产消息，等价缺陷今后会在离线单测里失败，而不是
在花掉五条案例的模型费用之后失败。

## Run H：第一次完成的 28 条全量运行（实测值，`scope=full`）

`golden-case:v9` 修好组件范围契约之后，`--all-cases` 的第二次尝试跑完了全部 28 条案例并写出报告。
这是本文第一组 `scope=full` 实测数字：`live-golden-eval:v2`、评分器 `golden-diagnosis-eval:v23`、
`code_revision=efe0cc5`、`case_coverage_rate=1.000`、`target_coverage_complete=true`，类别配额实测
8 / 10 / 4 / 3 / 3 与目标一致。

**先读这一段再读数字：这一轮端点大量超时，成绩主要由基础设施状况决定，不是模型推理质量结论。**
142 次调用里只有 92 次成功，44 次 `timeout`、3 次 `connection_error`、3 次 `output_invalid`；11 条案例
以 `planner_provider_error` 结束，其中 9 条一个工具都没执行（`logical_action_count=0`）。下面所有低分
指标都必须连着这个事实一起读。

| 指标 | Run G（smoke，3 条） | Run H（full，28 条） |
|---|---|---|
| `scope` / 分母 | `smoke` / 3 | **`full` / 28** |
| `case_coverage_rate` | 0.107 | **1.000** |
| `intent_accuracy` | 1.000 | 1.000 |
| `root_cause_top1_hit_rate` | 0.000 | 0.000（分母 21） |
| `root_cause_anchor_hit_rate` | 0.500（分母 2） | **0.071（分母 14）** |
| `necessary_action_coverage` | 0.667 | 0.702 |
| `evidence_source_coverage` | 0.667 | 0.696 |
| `fault_path_completeness` | 0.500 | 0.300 |
| `stop_reason_hit_rate` | 0.333 | 0.464 |
| `risk_level_hit_rate` | 0.667 | 0.500 |
| `citation_completeness` | 1.000 | 0.975 |
| `unsupported_critical_claim_rate` | 0.000 | 0.025 |
| `duplicate_action_rate` | 0.000 | 0.000 |
| `tool_attempt_success_rate` | 0.818 | 0.682 |
| `safe_degradation_rate` | 1.000 | 1.000 |
| `evidence_conflict_safe_resolution_rate` | 0.000 | 0.000 |
| `history_trigger_hit_rate` | 未列 | 1.000 |
| `history_recall_coverage` | 未列 | **0.000（见下）** |
| `accepted_report_rate` | 0.333 | 0.464（13/28） |
| 模型调用次数 | 14（8 成功、6 失败） | 142（92 成功、50 失败） |
| 总 token | 88,819 | 1,306,915（输入 1,257,151 / 输出 49,764） |
| 总耗时 | 375.3 s | 4271.3 s（71.2 min，均摊 152.5 s/案例） |

停止原因分布（28 条）：`planner_provider_error` 11、`evidence_sufficient` 9、`react_budget_exhausted` 3、
`evidence_insufficient` 3、`tool_unavailable_degraded` 1、`planner_output_invalid` 1。

### Run H 暴露的三件事（都不是模型质量结论）

**1. Planner 的 30 s 墙钟预算就是这一轮的主要瓶颈。** 成功的 49 次 Planner 调用耗时 6.5 s / 中位
15.3 s / 最大 **29.9 s**，而 `DATAOPS_CHAT_TIMEOUT_SECONDS=30`——分布的右尾正好压在墙上，33 次超时
全部落在 30.0–31.7 s。Auditor 侧同形：43 次成功为 7.2 s / 中位 24.7 s / 最大 89.2 s，11 次超时落在
90.0–90.4 s（`DATAOPS_AUDITOR_TIMEOUT_SECONDS=90`）。也就是说失败调用不是"端点拒绝服务"，而是
**这个推理模型在本项目的大 Structured Outputs schema 下，单次响应时间与配置的超时同量级**。调时限
之前不能把这一轮的低分归因给模型定位能力；调时限之后也必须重新测量，不能沿用本轮数字。

**2. 记忆类别的四个指标在真实链路上测不到东西，这是评测入口的前置缺陷。** `history_trigger_hit_rate`
是 1.000（三条案例都触发了召回），但 `history_recall_coverage` / `confirmed_only_recall_rate` /
`history_projection_pass_rate` / `realtime_priority_pass_rate` 全为 0.000，`forbidden_memory_hit_count=0`。
原因不是模型忽略历史：三条案例要求召回 `mem_golden_lts_upstream_history` 等 ID，而**真实库里没有
任何 confirmed 案例记忆**（实测 `case_memories` 只有 7 条 pending）。确定性 runner 把 Golden 标注
投影成 confirmed 匹配，真实 runner 走的是生产召回路径，于是 `recalled_memory_ids` 必然为空。
其中 `golden_memory_flashsync_stable_reference` 这一条链路完全正常（3 个工具、`evidence_sufficient`），
它的 0.000 同样只反映"库里没有可召回的历史"。**结论：live 模式缺一步 confirmed 记忆预置，在补上
之前这四个指标不具备发布意义，不能写成"历史召回失败"。**

> 记录时的 ID 已在 `golden-case:v10` 被替换：`mem_golden_*_history` 这类可读 ID 生产永远铸造不出来
> （唯一铸造路径是 `mem_{signature[:16]}`），因此现在的标注是 `mem_ebc78324034714d6`（LTS）、
> `mem_43fb5df2a9cf66da`（BDS）、`mem_4c0ab7ebba8e2aaa`（FlashSync），forbidden 为
> `mem_fdd472fc47cd485d` 与 `mem_079acbd5fc8f2fbc`。上文保留旧 ID 是因为它如实记录了 Run A–H 当时
> 读到的标注；这一轮的 0.000 与 ID 形状无关，换 ID 不改变任何已发布数字。

**3. `RiskLevel.HIGH` 在真实报告里一次都没出现。** `risk_level_hit_rate=0.500`，14 条未命中案例的
实测等级**全部是 low**（其中 3 条期望 high、11 条期望 medium）。`graph-seed:v12` 已让 high 在生产路径
可达，但可达不等于被选中：一个案例的实测等级取被召回方案节点的最大值，本轮大量案例在 Planner 首次
调用就失败、或没走到方案召回，因此这个 0.500 主要测的还是链路完成度，不是风险判定能力。

其余逐项：`root_cause_anchor_hit_rate=0.071` 的唯一命中是 `golden_bds_data_skew_single`（Top-1 引用了
`kn_root_cause_bds_data_skew`，同一条结论 `root_cause_top1_hit=false`——又一次两个口径分离的实测例证）；
`citation_completeness=0.975` 与 `unsupported_critical_claim_rate=0.025` 来自单一案例
`golden_flashsync_checkpoint_regression_single` 的 6 条关键结论里有 1 条缺实时支撑；
`duplicate_action_rate=0.000`、`safe_degradation_rate=1.000`、`forbidden_conflict_root_hit_count=0`
说明**去重、安全降级与禁止根因三条安全门禁在 28 条案例上全部守住了**——这是本轮唯一可以正面陈述的
结果，而它们恰好都是确定性规则负责的部分。

`evidence_conflict_safe_resolution_rate=0.000` 与 Run G 同因：`golden_bds_conflicting_partition_evidence`
再次以 `planner_provider_error` 零工具结束，三个矛盾 source 一个都没被观察。Run G2 已单独证明端点恢复
后该案例可复现安全处置，但那是 `scope=custom` 分母 1 的旁证，不能并入本轮。

## Run I：第一轮带历史预置的运行（实测值，`scope=custom`）

Run I 是 `--seed-history` 修复 ID 形状后的第一次真实执行，也是四个记忆类指标第一次拥有非空分母。
它刻意不跑 28 条：默认 smoke 集合里没有任何记忆案例，预置了也测不到东西，因此只选三条
`memory_recall` 案例（`scope=custom`、`case_count=3`、`case_coverage_rate=0.107`）。

运行元数据（实测值）：`live-golden-eval:v3`、`golden-case:v10`、`golden-diagnosis-eval:v23`、
`planner-react:v8`、`auditor-report:v2`、`bge-m3:v1`、代码版本 `a354591`、`metric_kind=measured`。
15 次模型调用**全部成功**（0 次失败、0 次结构化输出无效、0 次缺 usage），261,911 token，251.0 s
（均摊 83.7 s/案例）。这是目前唯一一轮端点全程稳定的运行，因此它的低分不能再归因给端点。

`history_seed` 实际写入：confirmed `mem_ebc78324034714d6` / `mem_43fb5df2a9cf66da` /
`mem_4c0ab7ebba8e2aaa`，pending `mem_fdd472fc47cd485d`，rejected `mem_079acbd5fc8f2fbc`，
向量空间 `bge-m3:v1` / 1024 维。

四个记忆类指标（实测值，分母为 3 条记忆案例）：

| 指标 | Run A–H | Run I | 读法 |
| --- | --- | --- | --- |
| `history_trigger_hit_rate` | 1.0000 | 1.0000 | 两轮都触发了召回 |
| `history_recall_coverage` | 0.0000（空分母） | **1.0000** | 三条案例各自的 required 记忆都被真实生产检索召回 |
| `confirmed_only_recall_rate` | 0.0000（空分母） | **1.0000** | pending 与 rejected 行确实存在且一条都没被召回 |
| `history_projection_pass_rate` | 0.0000（空分母） | 0.0000 | 真实的 0，但成因是报告被安全降级，见下 |
| `realtime_priority_pass_rate` | 0.0000（空分母） | 0.0000 | 同上；该指标要求 Top-1 根因命中允许标签 |
| `forbidden_memory_hit_count` | 0 | 0 | Run I 的 0 是真实的"禁止案例存在但没被召回" |

**前两项从"没测"变成了实测 1.0000，后两项从"没测"变成了实测 0.0000。这不是四项都提升。**

后两项的 0 有精确成因，`run_events` 可复算：三条案例的确定性规则预检都是 0 个问题
（`draft_created`），随后独立 Auditor 两轮都返回 `revise`（问题码 `unsupported_claim` /
`report_incomplete`），唯一一次返工用完后转 `safe_degraded`。降级报告按设计清空 `root_causes` 与
`similar_cases`（`app/reporting/revision.py`），于是 `history_projection_pass_rate` 要求的
"`similar_cases` 顺序等于召回顺序"必然失败，`realtime_priority_pass_rate` 要求的"Top-1 根因落在允许
标签内"也必然失败（`actual_top1_root_cause=None`）。**这两个指标因此同时测量"历史处理是否正确"和
"报告是否被放行"，Run I 测到的是后者不通过；不能把它们读成"模型把历史根因当成了本次结论"。**
同轮 `accepted_report_rate=0.0000`、`root_cause_top1_hit_rate=0.0000`、
`root_cause_anchor_hit_rate=0.0000`（`anchored_case_count=1`）都是同一个降级链条的结果。

降级边界本身守住了：三条 run 的 memory 事件全部是 `skipped_not_accepted`（`memory_id=null`），
degraded 报告一条都没有进入长期记忆。

同轮其余实测值：`intent_accuracy=1.0000`、`necessary_action_coverage=1.0000`、
`evidence_source_coverage=1.0000`、`citation_completeness=1.0000`、
`unsupported_critical_claim_rate=0.0000`、`duplicate_action_rate=0.0000`、
`safe_degradation_rate=1.0000`、`stop_reason_hit_rate=0.6667`、`tool_attempt_success_rate=0.6429`、
`risk_level_hit_rate=0.3333`、`fault_path_completeness=0.0000`。
`golden_memory_flashsync_stable_reference` 单独拖低两项：它跑出 8 个逻辑 Action、以
`react_budget_exhausted` 结束、该案例 `tool_attempt_success_rate=0.375`，另两条案例都是 3 个 Action、
`evidence_sufficient`、1.000。

Run I 不能与 Run A–H 放在同一列：案例集合不同（3 条记忆案例 vs 3 条 smoke / 28 条全量），且
`historical_root_cause` 在非冲突案例里就等于本次正确根因，记忆类案例的根因指标分母口径已经变了。

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

- `root_cause_top1_hit_rate` 实测仍为 0.000，Run G 也没有变。原因不是模型没给出根因：Run D 案例 1
  输出的根因文本已经准确说明 `partition_date` 用了 `20260713` 而任务声明要求 `yyyy-MM-dd`。评分器用
  **精确字符串相等**把报告根因与知识图节点名比较，一段自然语言句子永远不可能相等。`golden-case:v8`
  因此为 14 条案例引入了规范化根因锚点（知识图 `root_cause` 节点 ID），`golden-diagnosis-eval:v23` 新增
  `root_cause_anchor_hit_rate` 与它**并列**发布，Run G 实测 0.500（分母 2）。
  **这不是把 0.000 变成 0.500 的提升**：两个指标分母不同（Run G 里是 2 对 2，28 条全集里是 14 对 21）、
  口径不同（引用节点 ID 对根因文本相等），文本相等口径一个字都没改，也不会因为新指标存在而变好。
  要真正提升 `root_cause_top1_hit_rate`，仍然需要单独决定是否把它改成受控标签比较——那会是一次显式的
  口径变更，必须重新标注全部案例并作废旧数字，而不是用锚点悄悄替换。
- `risk_level_hit_rate` 在 Run D–G 都只到 0.667，缺口来自跨组件案例期望 high 而实测 low。**这条缺口
  的实现约束已在 `graph-seed:v12` 解除**：确定性 `_build_remediation_steps` 曾把所有知识方案硬编码成
  `RiskLevel.MEDIUM`，`RiskLevel.HIGH` 在生产路径上不可达，因此旧上限测的是实现缺陷而不是模型能力；
  现在风险等级由 `solution` / `sop` 节点的 `remediation_risk_level` 声明（三个 high、五个 medium），
  `tests/unit/test_reporting.py` 与 `tests/unit/test_fixture_registry.py` 分别锁定"high 能穿过生产
  报告路径"和"28 条案例期望的每个等级都可达"。但**上表的 0.667 仍然是最后一次实测值，不得改写**：
  真实模型下的新值要等下一次 live 运行才能测量，而且一个案例的实测等级取被召回方案节点的最大值，
  仍然依赖检索是否选中那个 high 方案——解除实现约束不等于指标自动达标。
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
  主动收口"，需要在 Prompt 里进一步强化剩余步数临界时的收口判断。（Run H 的 0.464 分母是 28，
  其中 11 条根本没走到收口就 `planner_provider_error`，两者不可直接比较。）
- **上一条的归因在 `mcp-transport:v1` 切片的一次手工 live 诊断后被修正：病因是预算算术，不是模型的
  收口判断。** 那次 run（`/demo` 单案例，`gpt-5.6-sol`，端到端 96.7 s）用 3+3+2 三个并行批次刚好填满
  8 步预算，8 次工具调用全部成功。控制器的步数检查排在模型调用**之前**，所以恰好用满预算的 Planner
  拿不到最后那次决策机会——它没有"没能主动收口"，而是没有回合可用。下游连锁与 Run I 一致：报告基于
  "调查未完成"起草 → Auditor 两轮 `revise`（`report_incomplete`）→ 唯一一次返工预算用尽 →
  `safe_degraded` → `root_causes` 为 0。因此"让 Prompt 更早收口"是反方向的修法：那次 run 的最后两个
  Action 取的正是必需证据，提前收口只会用证据覆盖率换一个好看的 `stop_reason`。已做的处置是把
  `DATAOPS_MAX_REACT_STEPS` 默认值从 8 提到 10（理由链条见 `docs/learning/08-有界ReAct循环.md`
  §8.8.1），**这只降低命中概率、不消除失败面**：恰好用满 10 步仍会撞上同一条路。结构性解法已在
  `langgraph-react-loop:v4` 落地——取证预算与收口额度拆成两笔，预算打满时控制器额外发放一次批次上限
  为 0 的收口回合（`planner-react:v9` 的 `{closing_turn}`），因此"恰好用满预算"不再等于"没有回合可
  用"。**但这条闭合只有测试证据，没有 live 实测证据**：v9/v4 尚未跑过任何真实模型运行。同时这意味着
  **Run A–I 的全部数字都是在 8 步预算、v8/v3 契约下测得的**；预算改动与循环语义改动本身都不构成任何
  指标的改善证据，`stop_reason_hit_rate` 的新值必须等下一次 live 运行实测，上表数字一律不得改写。
- **10 步预算下的单案例手工复测（不是评测运行，不得计入上表任何指标）。** 同一条跨组件案例
  （`golden_cross_chain_pk_conflict`，同一段合成路由元数据，经 compose 生产装配 + Streamable HTTP
  网关 + PostgreSQL Worker），`run_83294e77ae9f4d11`，端到端 63.4 s：三个 3 并行批次共 9 个 Action
  全部成功（九个工具各一次），`react_step=9`，第四次 Planner 决策因为 10 步预算仍有余量而得以发生
  （那时还没有 v4 的收口回合，靠的是余量而不是保留额度），并自报
  `stop_reason=evidence_sufficient`，Auditor 首轮即 `accept`（0 问题、`retry_count=0`，未触发返工或
  降级），报告含 2 条根因（其中一条是 FlashSync 主键冲突）、9 条证据，Golden 声明的三条
  `required_evidence_sources` 全部命中，记忆候选 `staged` 待确认。这条观测支持"8 步下缺的是最后那次
  决策机会而不是模型能力"，也正是 v4 把它变成保留额度的依据，但**分母是 1**：它既不能替代
  `stop_reason_hit_rate` 的重测，也不构成任何百分比。
  同一次运行里 `risk_level` 仍是 low（Golden 期望 medium，且唯一一条处置建议是"继续补齐证据"的通用
  步骤），说明 `RiskLevel` 那条缺口与步数预算无关，依旧未闭合。
- **Planner 超时配置与该模型的响应分布同量级，这是 Run H 最大的单一失分来源。** 实测成功调用中位
  15.3 s、最大 29.9 s，而 `DATAOPS_CHAT_TIMEOUT_SECONDS=30`，33/86 次 Planner 调用在 30 s 墙上失败。
  调大时限或缩小单次 schema 都可能改善，但**任何调整之后本轮全部数字作废、必须重测**；在重测之前
  不能把 Run H 的低分归因给模型定位能力，也不能把预期的改善写成成绩。
- **live 模式缺少 confirmed 记忆预置，记忆类别四个指标暂不具备发布意义。** Run H 实测
  `history_trigger_hit_rate=1.000` 但 `history_recall_coverage` / `confirmed_only_recall_rate` /
  `history_projection_pass_rate` / `realtime_priority_pass_rate` 全为 0.000，原因是真实库里只有 7 条
  pending 记忆、没有任何 confirmed 案例。确定性 runner 把 Golden 标注投影成 confirmed 匹配，真实 runner
  走生产召回路径，因此这四个指标测的是"库里有没有历史"，不是"模型会不会用历史"。补一步显式的
  confirmed 记忆预置（并保持 pending/rejected 禁止命中不变）之前，这四个数字只能标注为前置缺失。
  **Run I 已经补上这一步**：前两项实测 1.0000，后两项实测 0.0000，前置缺失这条对召回类指标不再成立。
- **`history_projection_pass_rate` 与 `realtime_priority_pass_rate` 把两件事混在一个数字里，这是 Run I
  暴露的口径缺陷。** 前者要求报告 `similar_cases` 的顺序等于召回顺序，后者要求 Top-1 根因落在允许标签
  内；而安全降级报告按设计清空 `root_causes` 与 `similar_cases`。因此只要报告没被放行，这两项必然为 0，
  与模型如何处理历史无关。Run I 三条案例全部走到 `safe_degraded`（Auditor 两轮 `revise`，规则预检 0
  问题），实测就是这个情形。要把它们变成纯粹的历史处理指标，需要在 accepted 报告子集上另立分母——那是
  一次显式口径变更，必须重新发布定义，不能事后把 Run I 的 0 解释成别的意思。
- **Run I 的 `accepted_report_rate=0.0000` 说明这三条记忆案例的报告一次都没被放行。** 问题码是
  `unsupported_claim` 与 `report_incomplete`，且两轮 Auditor 判定一致、确定性规则预检 0 问题。这是目前
  唯一一轮端点全程稳定（15/15 调用成功）的运行，因此这个 0 不能再归因给超时；但分母只有 3，也不能
  外推成"报告放行率很低"。
- **`RiskLevel.HIGH` 在真实报告中尚未被观测到一次。** Run H 的 14 条未命中案例实测等级全部是 low
  （3 条期望 high、11 条期望 medium）。`graph-seed:v12` 解除的是实现上限，不是检索选中率；这条缺口
  要等 Planner 侧链路完成度恢复之后才能重新测量。Run I 同形：`risk_level_hit_rate=0.3333`，两条未命中
  案例实测等级都是 low。

## 真实执行路径

命令在同一进程进入 FastAPI lifespan，启动并审计生产依赖，然后为每条案例创建独立 PostgreSQL
session：

```text
load golden-case:v10
  -> validate local settings and select case IDs
  -> FastAPI lifespan validates Fixture / Prompt / Graph / real MCP discovery
  -> optional live-history-seed writes confirmed / pending / rejected case memories
  -> PostgreSQL GraphRAG retrieves an Evidence Bundle
  -> Planner Structured Outputs chooses Action
  -> LangGraph executes the Action through stdio MCP
  -> Observation returns to the bounded Planner loop
  -> deterministic report policy + independent Auditor
  -> audited memory staging and persisted run/events/checkpoint
  -> golden-diagnosis-eval:v23 scores the public DiagnosisRunResult
  -> live-golden-eval:v3 aggregates safe model-call telemetry
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

要测量四个记忆类指标，需要额外追加 `--seed-history`（它会向 `case_memories` 真实写入预置行，因此
只在本地测试库上使用）：

```powershell
.venv\Scripts\python -m app.evaluation.live_golden `
  --code-revision '<git commit>' `
  --all-cases --seed-history `
  --output 'live-golden-seeded.json'
```

## 当前不能宣称什么

- 不能把三案例 smoke 外推到 28 条或生产故障分布；`target_coverage_complete=false` 必须一起给出。
- 不能拿 Run H 的 `scope=full` 当成"28 条真实模型成绩"对外宣称：那一轮 142 次调用有 50 次失败
  （44 次超时），11 条案例零工具结束，**`scope=full` 的实测数字现在存在，但只能读作"链路在全量案例上
  完整跑通并被评分"**，不是模型能力基线。首次 `--all-cases` 尝试因组件范围契约缺陷在第六条案例中止且
  不写报告，那次仍然没有任何可发布数字（详见"被中止的首次全量运行"一节）。
- 不能宣称达成 P95 ≤ 30 s：实测三案例平均端到端 Run D 约 58 s、Run F 约 75.7 s、Run G 约 125.1 s
  （Run G 含超时与重试，属端点不可用事件，但仍不构成达标证据）；Run H 全量均摊约 152.5 s/案例。
  该目标仍是设计目标值。
- 不能把 Run H 记忆类别的四个 0.000 说成"历史召回不work"：真实库里没有 confirmed 案例记忆，那是
  评测前置缺失，`history_trigger_hit_rate` 同轮实测 1.000。
- 不能因为 Run I 让四个记忆指标有了分母就宣称"记忆通道已达标"：只有
  `history_recall_coverage` 与 `confirmed_only_recall_rate` 实测 1.0000，
  `history_projection_pass_rate` 与 `realtime_priority_pass_rate` 实测 0.0000，而且分母只有 3 条记忆
  案例。Run I 也不能与 Run A–H 同列，因为预置会把 `historical_root_cause` 送进记忆类案例的历史上下文。
- 不能把 Run I 的两个 0.0000 说成"模型把历史结论当成了本次根因"：三条案例都在 Auditor 两轮 `revise`
  后安全降级，降级报告按设计清空 `root_causes` 与 `similar_cases`，这两个指标同时受"报告是否被放行"
  影响，Run I 测到的是后者不通过。
- 不能把 Run H 的 `risk_level_hit_rate=0.500` 说成风险判定能力：未命中案例实测等级全部是 low，多数
  案例没走到方案召回。
- 不能把 Run G 的 `root_cause_anchor_hit_rate=0.500` 当成模型定位能力的成绩：分母只有 2
  （`anchored_case_count=2`），且另一条未命中是因为步数预算耗尽后报告里根本没有根因。
- 不能把 Run F 说成"重试已在真实端点验证"：那次运行没有出现瞬时失败，重试路径一次都没执行。被丢弃的
  terra 探测运行只证明退避会执行且遥测粒度正确，两次尝试都失败，救回效果仍未测量。
- 不能把 `root_cause_top1_hit_rate=0` 说成"模型找不到根因"，也不能把它悄悄改口径后当成提升。
- 不能把 MockTransport 的固定 15 token 响应当作模型成本实测。
- 不能把确定性 Golden runner 的 28/28 满分当作 Planner/Auditor 质量。
- 不能因为 token/耗时可观测就保存 Prompt、模型原始响应或 Thought。


