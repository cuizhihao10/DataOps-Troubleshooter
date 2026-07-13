# Golden 诊断确定性回归实测报告

本文记录 `golden-diagnosis-eval:v16` 在当前 23 条 `golden-case:v7` 合成案例上的可重复实测。产品目标是 28
条，因此当前覆盖率为 `23/28 = 82.14%`，`target_coverage_complete=false`。下列满分项只证明确定性
脚本、强类型诊断结果和评分管线遵守当前标注，不能外推为真实 LLM 的意图识别或根因诊断准确率。

## 1. 被测边界

数据来自 `data/fixtures/golden_cases.json` 与 `data/fixtures/scenarios/`，全部是合成/Mock 内容。测试
runner 按每条案例的必要只读工具回放已校验 Fixture 响应，构造生产 `ToolEvent`、`Evidence`、
`RetrievedPath`、`DiagnosisReport`、`ReactRunResult`、`ReportRunResult` 和 `DiagnosisRunResult`。评分器只读取这些公开强
类型结果，不读取 Fixture 答案、Prompt、模型原始输出或 Thought。

这个 runner 是“预期通过”的确定性回归基线，不是 Planner/Auditor 模型评测：

- Planner 的必要 Action 与允许根因由脚本按 Golden 标注选择；
- Auditor 使用确定性 accept 结果，真实规则门禁和独立 Auditor 增量由其他 suite 验证；
- MCP 响应内容来自版本化 Fixture，但本测试不启动 stdio MCP 子进程；真实协议边界由
  `tests/integration/test_mcp_protocol.py` 与 `test_react_loop_mcp.py` 验证；
- PostgreSQL、pgvector、GraphRAG 和长期记忆召回质量不在本层重复测量。

这样的分层避免把多个变量混成一个分数：本层先锁定“Golden 标注 → 顶层结果 → 指标”数据流，后续
再将相同评分器替换为真实模型/完整运行时 runner。

23 条案例使用 13 个 scenario Fixture。单组件、跨组件、模糊补参与记忆视角可以选择同一事实快照中的不同工具
子集而不复制 MCP 返回；资源耗尽案例则使用独立环境，避免所有跨组件问题都映射到主键冲突。案例
数量表达“问题与预期行为”，Fixture 数量表达“可重放事实环境”，两者可以独立扩展。

当前类别配额如下：

| 类别 | 当前 | 产品目标 | 尚缺 |
|---|---:|---:|---:|
| 单组件明确故障 | 8 | 8 | 0 |
| 跨组件故障 | 5 | 10 | 5 |
| 模糊或证据不足 | 4 | 4 | 0 |
| 工具异常或证据冲突 | 3 | 3 | 0 |
| 长期记忆召回 | 3 | 3 | 0 |

## 2. 指标定义

| 指标 | 计算方式 | 空分母语义 |
|---|---|---|
| 意图命中率 | `AgentState.intent == expected_intent` 的案例均值 | 不允许空案例集 |
| 必要 Action 覆盖率 | 实际 `ToolEvent.tool_name` 覆盖标注工具的比例 | 无必要工具时为 1，但当前没有该类案例 |
| 根因 Top-1 | 最终已审计报告首个根因是否属于允许集合 | 只在 16 条有根因案例上计算 |
| Evidence source 覆盖率 | 本次 `Evidence.source_id` 覆盖标注来源的比例 | 无必要来源时为 1 |
| 故障链路完整率 | 必要节点/关系在 `RetrievedPath` 中有序出现，且同一 `path_id` 被最终 `fault_chain` 引用 | 只在有路径标注案例上计算 |
| 停止原因命中率 | ReAct 最终 `stop_reason` 是否属于允许集合 | 无空集合标注 |
| 关键结论引用完整率 | 根因、链路和高风险建议的引用是否均指向现有 Evidence、Graph path 或 confirmed case | 无关键结论时为 1 |
| 无依据关键结论率 | 引用缺失/无效的关键结论数除以关键结论总数 | 无关键结论时为 0 |
| 重复 Action 率 | 同一 run 内相同工具与参数的额外 `attempt=1` 调用占逻辑 Action 数 | 合法 `attempt=2` 瞬时重试不算重复 |
| 工具尝试成功率 | `ToolEvent.response.ok=true` 尝试数除以全部尝试数 | 无工具尝试时为 1 |
| 风险命中率 | 报告最高建议风险是否等于案例标注 | 无建议按 low 处理 |
| 安全降级率 | 无允许根因案例同时满足“无根因输出”和“公开不确定性” | 只在 7 条无根因案例上计算 |
| 证据冲突安全处置率 | 标注冲突来源全部被观察，禁止根因零命中，并满足无根因与 uncertainty 义务 | 只在 1 条成功响应冲突案例上计算 |
| 禁止冲突根因命中数 | 最终报告命中任一单侧禁止根因的次数 | 目标为零 |
| 必要历史召回覆盖率 | required confirmed memory ID 被 raw recall 命中的比例 | 只在 3 条记忆案例上计算 |
| 历史投影通过率 | 最终 `similar_cases` 是否按顺序完整投影 raw recalled IDs | 只在 3 条记忆案例上计算 |
| 实时事实优先率 | 冲突历史根因未进入报告，且每个当前根因至少引用本次 TOOL Evidence | 只在 3 条记忆案例上计算 |
| 禁止记忆命中数 | forbidden pending/rejected/错误案例 ID 的实际命中数 | 目标为零 |

引用完整率只验证引用 ID 的结构完整性，不判断引用内容是否在语义上支持结论；语义支持度继续由
Auditor 和人工抽查承担。工具成功率包含当前故意注入的空结果、超时和权限拒绝，不能直接与产品表中
“不含故意异常”的 ≥95% 目标比较。

## 3. 本次实测结果

固定代码与数据版本下，`tests/integration/test_golden_diagnosis_evaluation.py` 得到：

| 实测指标 | 当前值 | 样本边界 |
|---|---:|---|
| Golden Case 覆盖率 | 82.14% | 23/28，未完成 |
| 意图命中率 | 100% | 23 条确定性脚本 |
| 根因 Top-1 命中率 | 100% | 16 条有根因案例 |
| 必要 Action 覆盖率 | 100% | 23 条，共 69 个逻辑 Action；1 条零 Action 补参案例 |
| Evidence source 覆盖率 | 100% | 当前标注来源 |
| 故障链路完整率 | 100% | 15 条适用案例、21 条必要路径 |
| 停止原因命中率 | 100% | 23 条 |
| 关键结论引用完整率 | 100% | 结构化 ID 检查 |
| 无依据关键结论率 | 0% | 结构化 ID 检查 |
| 重复 Action 率 | 0% | 合法重试排除后 |
| 工具尝试成功率 | 89.86% | 69 次尝试、62 次成功，含 7 条故意失败响应 |
| 风险等级命中率 | 100% | 23 条 |
| 安全降级率 | 100% | 7 条无根因案例 |
| 证据冲突安全处置率 | 100% | 1 条、3 个成功但互相矛盾的 BDS Observation |
| 禁止冲突根因命中数 | 0 | 两个单侧结论均未进入报告 |
| 历史触发命中率 | 100% | 3 条记忆案例 |
| 必要历史召回覆盖率 | 100% | 3 条 required confirmed memory |
| confirmed-only 召回率 | 100% | 3 条记忆案例 |
| 历史报告投影通过率 | 100% | 3 条记忆案例 |
| 实时事实优先通过率 | 100% | 含 1 条历史根因冲突案例 |
| 禁止记忆命中数 | 0 | pending/rejected/错误 ID 均未出现 |
| 报告接受率 | 100% | 确定性 Auditor 脚本 |

第一条新增跨组件案例通过真实 MCP 依次读取 LTS 状态/拓扑与 BDS 状态/日志/表信息，形成“LTS 上游未就绪
→ 依赖 BDS 聚合作业 → BDS 等待缺失分区”的公开证据链。Schema 还会拒绝只含单组件工具或没有
`required_fault_paths` 的跨组件标注，避免用 category 标签虚增配额。

第二条新增案例在 BDS→FlashSync 边界执行六项真实 MCP Action：BDS 停在 source_read、目标分区落后，
FlashSync 吞吐为零且日志记录脱敏主键冲突，一致性差异又与积压数量相同。最终报告必须同时引用任务
依赖、同步产出数据集和“积压→主键冲突→解决方案”三条路径，避免只命中根因文本却缺少传播链。

第三条跨组件案例使用新的 `cross_lts_bds_resource_exhaustion` Fixture。LTS 状态、日志和拓扑证明失败
来自 BDS 上游超时；BDS 状态与日志证明 CPU/内存饱和、spill 和执行器丢失；表信息同时证明分区按时
到达、数据量接近基线，日志倾斜比例仅 1.09。这个反证链把资源耗尽与缺分区、异常输入量和数据倾斜
区分开，并复用已经实测的 LTS `DEPENDS_ON` BDS 组件路径，不为单条案例制造未验证的新图节点。

第四条跨组件案例使用独立 `cross_customer_profile_schema_propagation` Fixture，并把调查预算固定为六项
只读 Action。LTS 状态和拓扑给出 600 条上游缺口以及 LTS→BDS→FlashSync 任务身份；BDS 状态与表信息
证明输入分区已经建立、资源未饱和，但 5000 条预期记录只到达 4400 条；FlashSync 日志和一致性再给出
源 v12/映射 v11、`customer_tier` 未映射、600 条拒绝/解析失败/目标缺失且零重复。同一个 600 在三层
闭合后，报告才能把根因放在 FlashSync 映射边界，而不是 LTS 调度或 BDS 资源。`graph-seed:v6` 以四个
任务/数据集节点和八条显式边连接该事实环境，PostgreSQL 分别验证任务依赖两跳路径以及
`MANIFESTS_AS → CAUSED_BY` 路径；Golden 还要求既有 v5 根因到验证方案路径进入最终报告。

第五条单组件案例使用独立 `lts_parameter_validation_failure` Fixture。状态确认配置阶段三次重试耗尽，
日志给出 `INVALID_PARTITION_DATE` 和 `partition_date` 期望格式，拓扑则明确两个上游均已就绪。第三项是
排除“上游未就绪”的反证，不能因为它不支持候选根因就从报告引用面删除。`graph-seed:v2` 新增
“参数校验失败 → 分区日期参数格式错误 → 校验并修正运行参数”两跳路径；PostgreSQL 集成测试通过真实
pgvector 种子召回和递归 CTE 得到该路径，Golden 报告必须引用同一有序节点/关系链。

第六条单组件案例使用独立 `bds_data_skew` Fixture。状态显示 16 个执行器仍在线，但聚合阶段停在
83% 达 1080 秒；日志给出 `DATA_SKEW_DETECTED`、9.6 倍热点分桶与 27 次溢写；表信息同时证明当天
分区已就绪，318 万总行数仍处于 300–340 万基线范围。正常总量是排除输入暴增和缺分区的反证，不能
被当作“与根因无关”而丢弃。`graph-seed:v3` 新增“执行阶段长尾 → 数据倾斜 → 拆分热点并重新平衡”
路径；真实 MCP、pgvector 种子召回、递归 CTE 与 Golden fault_chain 使用相同稳定节点和关系类型。

第七条单组件案例使用独立 `flashsync_checkpoint_regression` Fixture。延迟工具显示当前 offset 87220
比已提交位点 88420 落后 1200，积压也是 1200；日志明确 `CHECKPOINT_REGRESSION`、旧快照来源并阻止
自动重放；一致性抽检再确认目标端缺少 1200 条且没有重复记录。三个独立数值闭合后才允许输出
“检查点位点回退”，不能把普通延迟或主键冲突套用到本次。`graph-seed:v4` 的恢复方案要求先备份差异
与检查点、核对幂等边界，再小批量重放和复查，因此 Golden 风险必须命中 high，而不是把只读诊断
误写成可直接执行的自动修复。

第八条单组件案例使用独立 `flashsync_schema_mapping_outdated` Fixture。延迟工具显示源 Schema v12、
映射 v11、零吞吐和 600 条积压；日志明确 `SCHEMA_MAPPING_OUTDATED`、未映射字段 `customer_tier` 与
600 条拒绝；一致性抽检再确认 600 条目标缺失、600 次 Schema 解析失败且零重复。版本差和两个数量
闭合后才允许输出“字段映射版本滞后”，避免套用主键冲突或检查点回退。`graph-seed:v5` 的方案要求
比对源目标 Schema、预览默认值与字段语义、小批量回放并复查一致性；该案例完成单组件 8/8 配额。

第二条模糊/证据不足案例只提供“BDS 任务运行很慢”，缺少资源 ID 和时间窗，因此必须走零 MCP Action
补参边界。`golden-case:v7` 要求
零工具案例只能属于该类别，且 paths、Evidence source 和 allowed root 必须为空；Golden runner 从
引用 Scenario 取得 BDS 组件上下文，但不回放任何工具结果。生产 ReAct 的独立测试同时证明
`need_user_input` 会以 `missing_resource_id`、`react_step=0` 停止且 executor 未被调用，避免无标识探测
或猜测根因。

第三条模糊/证据不足案例使用 `flashsync_incomplete_root_cause_evidence` Fixture。延迟工具确认 2100 秒
延迟、74 条积压和零吞吐；一致性工具确认 74 条目标端缺失，但日志工具返回不可重试 `EMPTY_RESULT`。
前两项只证明症状一致，不能说明差异由主键冲突、权限、位点或其他原因造成。真实 MCP 测试要求失败
日志不生成 Evidence；Golden 报告必须保留两个成功 source ID、以 `evidence_insufficient` 停止并保持空根因。

第四条模糊/证据不足案例复用 `lts_empty_result` 的同一事实窗口，但要求依次检查状态、日志和依赖拓扑。
状态与日志分别返回不可重试 `EMPTY_RESULT`，拓扑则返回可重试 `TIMEOUT`；真实 MCP 执行器对前两项各
保留一个事件，对拓扑保留 attempt 1/2 两个事件。错误消息不是业务事实，因此所有 Observation 的
Evidence 与 observation refs 必须为空，Golden 报告只能公开证据缺口并以 `evidence_insufficient` 停止。

负向测试会注入缺失 Action、无效引用猜测根因、未投影 raw memory、只引用 confirmed memory 但与本次
Observation 冲突的旧根因，以及“保留全部冲突 Evidence、引用有效 ID，却武断选择禁止根因并清空
uncertainties”的报告。最后一种结果的结构引用完整率仍为一，但证据冲突安全处置必须失败，证明专用
指标检查的是事实冲突边界，不是 citation 指标的重复包装。真实 MCP 集成测试还会证明三条矛盾事实均
以 `ok=true` 穿过 stdio 协议，协议层不会静默调和或丢弃任一 Observation。

## 4. 复现命令

```powershell
.venv\Scripts\python -m pytest -q tests/integration/test_golden_diagnosis_evaluation.py
```

统一快速评测会同时运行本层并明确跳过 PostgreSQL 层：

```powershell
.venv\Scripts\python -m app.evaluation --skip-postgres
```

完整统一评测仍需配置合成测试数据库 URL。只有 suite 本次 pytest 通过，统一报告才发布 manifest 中的
实测快照；失败、跳过或 blocked 均隐藏旧数字。

## 5. 未覆盖风险与下一步

- 还缺 5 条案例：单组件、模糊/证据不足、长期记忆和工具异常/证据冲突类别已满额，只剩跨组件 5 条；
- 当前没有自然语言意图路由器，API 仍要求调用方显式提供 intent/components；
- 当前确定性 runner 不衡量真实 LLM Planner/Auditor 的语义质量、token 成本或端到端 P95；
- 当前只有 15 条案例共 21 条必要路径，路径类别仍是小样本；
- 后续应把路径标注扩展到新增的适用案例，再实现可配置真实运行时 runner，并把模型、Prompt、数据集和
  代码版本写入结果快照。
