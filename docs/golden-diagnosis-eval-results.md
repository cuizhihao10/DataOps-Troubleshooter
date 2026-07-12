# Golden 诊断确定性回归实测报告

本文记录 `golden-diagnosis-eval:v1` 在当前 5 条合成 Golden Cases 上的可重复实测。产品目标是 28
条，因此当前覆盖率只有 `5/28 = 17.86%`，`target_coverage_complete=false`。下列满分项只证明确定性
脚本、强类型诊断结果和评分管线遵守当前标注，不能外推为真实 LLM 的意图识别或根因诊断准确率。

## 1. 被测边界

数据来自 `data/fixtures/golden_cases.json` 与 `data/fixtures/scenarios/`，全部是合成/Mock 内容。测试
runner 按每条案例的必要只读工具回放已校验 Fixture 响应，构造生产 `ToolEvent`、`Evidence`、
`DiagnosisReport`、`ReactRunResult`、`ReportRunResult` 和 `DiagnosisRunResult`。评分器只读取这些公开强
类型结果，不读取 Fixture 答案、Prompt、模型原始输出或 Thought。

这个 runner 是“预期通过”的确定性回归基线，不是 Planner/Auditor 模型评测：

- Planner 的必要 Action 与允许根因由脚本按 Golden 标注选择；
- Auditor 使用确定性 accept 结果，真实规则门禁和独立 Auditor 增量由其他 suite 验证；
- MCP 响应内容来自版本化 Fixture，但本测试不启动 stdio MCP 子进程；真实协议边界由
  `tests/integration/test_mcp_protocol.py` 与 `test_react_loop_mcp.py` 验证；
- PostgreSQL、pgvector、GraphRAG 和长期记忆召回质量不在本层重复测量。

这样的分层避免把多个变量混成一个分数：本层先锁定“Golden 标注 → 顶层结果 → 指标”数据流，后续
再将相同评分器替换为真实模型/完整运行时 runner。

## 2. 指标定义

| 指标 | 计算方式 | 空分母语义 |
|---|---|---|
| 意图命中率 | `AgentState.intent == expected_intent` 的案例均值 | 不允许空案例集 |
| 必要 Action 覆盖率 | 实际 `ToolEvent.tool_name` 覆盖标注工具的比例 | 无必要工具时为 1，但当前没有该类案例 |
| 根因 Top-1 | 最终已审计报告首个根因是否属于允许集合 | 只在 2 条有根因案例上计算 |
| Evidence source 覆盖率 | 本次 `Evidence.source_id` 覆盖标注来源的比例 | 无必要来源时为 1 |
| 停止原因命中率 | ReAct 最终 `stop_reason` 是否属于允许集合 | 无空集合标注 |
| 关键结论引用完整率 | 根因、链路和高风险建议的引用是否均指向现有 Evidence、Graph path 或 confirmed case | 无关键结论时为 1 |
| 无依据关键结论率 | 引用缺失/无效的关键结论数除以关键结论总数 | 无关键结论时为 0 |
| 重复 Action 率 | 同一 run 内相同工具与参数的额外 `attempt=1` 调用占逻辑 Action 数 | 合法 `attempt=2` 瞬时重试不算重复 |
| 工具尝试成功率 | `ToolEvent.response.ok=true` 尝试数除以全部尝试数 | 无工具尝试时为 1 |
| 风险命中率 | 报告最高建议风险是否等于案例标注 | 无建议按 low 处理 |
| 安全降级率 | 无允许根因案例同时满足“无根因输出”和“公开不确定性” | 只在 3 条无根因案例上计算 |

引用完整率只验证引用 ID 的结构完整性，不判断引用内容是否在语义上支持结论；语义支持度继续由
Auditor 和人工抽查承担。工具成功率包含当前故意注入的空结果、超时和权限拒绝，不能直接与产品表中
“不含故意异常”的 ≥95% 目标比较。

## 3. 本次实测结果

固定代码与数据版本下，`tests/integration/test_golden_diagnosis_evaluation.py` 得到：

| 实测指标 | 当前值 | 样本边界 |
|---|---:|---|
| Golden Case 覆盖率 | 17.86% | 5/28，未完成 |
| 意图命中率 | 100% | 5 条确定性脚本 |
| 根因 Top-1 命中率 | 100% | 2 条有根因案例 |
| 必要 Action 覆盖率 | 100% | 5 条，共 10 个逻辑 Action |
| Evidence source 覆盖率 | 100% | 当前标注来源 |
| 停止原因命中率 | 100% | 5 条 |
| 关键结论引用完整率 | 100% | 结构化 ID 检查 |
| 无依据关键结论率 | 0% | 结构化 ID 检查 |
| 重复 Action 率 | 0% | 合法重试排除后 |
| 工具尝试成功率 | 70% | 10 次尝试，含 3 条故意异常/空结果 |
| 风险等级命中率 | 100% | 5 条 |
| 安全降级率 | 100% | 3 条无根因案例 |
| 报告接受率 | 100% | 确定性 Auditor 脚本 |

负向测试还会在结构合法结果中注入缺失 Action 和无效引用猜测根因，确认评测器分别把必要 Action、
安全降级和引用完整率降为零，证明 `accept` 标签不会掩盖客观违规。

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

- 还缺 23 条案例：单组件、跨组件、模糊输入、证据冲突和长期记忆类别均未达到产品配额；
- 当前没有自然语言意图路由器，API 仍要求调用方显式提供 intent/components；
- 当前确定性 runner 不衡量真实 LLM Planner/Auditor 的语义质量、token 成本或端到端 P95；
- Golden schema 尚未标注产品要求的必要故障路径节点/边，链路完整率暂不能诚实计算；
- 后续应先扩展 Golden schema 与案例，再实现可配置真实运行时 runner，并把模型、Prompt、数据集和
  代码版本写入结果快照。
