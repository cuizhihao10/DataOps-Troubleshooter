# 历史案例端到端影响消融实测记录

本文记录 `history-impact-eval:v1` 在当前仓库、固定合成案例和确定性 LangGraph 集成 runner 下得到的
实测值。它评估长期记忆加入后实际 Action、最终报告和实时事实优先门禁是否变化，不代表付费 LLM
推理质量、生产故障准确率或 PostgreSQL 召回效果，也不能外推到其他模型、Prompt 或数据集。

## 1. 评测条件

| 项目 | 实测配置 |
|---|---|
| 契约 | `history-impact-eval:v1` |
| 评测文件 | `data/evals/history_impact_cases.json` |
| 案例数 | 3 条脱敏合成诊断：Action 引导、实时冲突保护、同根因稳定参考 |
| 对照组 | `memory_off`：`history_trigger=not_requested`，不得查询或携带历史案例 |
| 实验组 | `memory_on`：`history_trigger=user_requested`，每例召回 1 条 confirmed 案例 |
| 编排 | 真实 `BoundedReactLoop` → `AuditedReportWorkflow` → `AuditedDiagnosisWorkflow` |
| Planner/Auditor | 确定性协议替身；不访问付费模型，不记录 Thought |
| 工具执行 | 合成成功响应，经生产 `normalize_observation` 生成 Evidence 与 ToolEvent |
| 历史搜索 | 确定性 confirmed match；真实 pgvector 召回由 `memory-recall-eval:v1` 单独实测 |
| 指标类型 | `measured`（实测值） |

两组共享同一 case 的 user query、组件、scenario、当前根因和初始 TOOL Evidence，唯一批准变量是
history trigger。评测器还会反向校验：off 组若含历史、on 组未达到最小命中或两组改变 query，立即
失败，不生成不完整平均值。

## 2. 三条案例实测值

| 案例 | 模式 | 实际工具 | 必要 Action 覆盖 | 意外 Action 率 | Top-1 根因命中 | TOOL 引用率 | 历史投影 | 冲突保护 |
|---|---|---|---:|---:|---:|---:|---|---|
| `history_impact_action_guidance` | Memory off | `lts.get_task_status` | 0.0 | 1.0 | 1.0 | 1.0 | 空历史 | 不适用 |
| `history_impact_action_guidance` | Memory on | `lts.get_dependency_topology` | 1.0 | 0.0 | 1.0 | 1.0 | 通过 | 不适用 |
| `history_impact_realtime_conflict_guard` | Memory off | `lts.get_task_status` | 1.0 | 0.0 | 1.0 | 1.0 | 空历史 | 不适用 |
| `history_impact_realtime_conflict_guard` | Memory on | `lts.get_task_status` | 1.0 | 0.0 | 1.0 | 1.0 | 通过 | 通过 |
| `history_impact_stable_reference` | Memory off | `bds.get_task_log` | 1.0 | 0.0 | 1.0 | 1.0 | 空历史 | 不适用 |
| `history_impact_stable_reference` | Memory on | `bds.get_task_log` | 1.0 | 0.0 | 1.0 | 1.0 | 通过 | 不适用 |

Action 引导案例的历史上下文使确定性 Planner 从宽泛状态查询改为 fixture 标注的必要依赖拓扑检查。
冲突案例中，历史根因是“上游数据未按时就绪”，本次 TOOL Evidence 支持的根因是“LTS 资源队列
配额不足”；最终报告保留本次根因，similar case 的 differences 明确根因不一致，pitfall warning
明确禁止直接复用历史修复方案。

## 3. Macro 汇总实测值

| 指标 | Memory off | Memory on | 差值 |
|---|---:|---:|---:|
| 必要 Action Macro 覆盖率 | 0.6667 | 1.0000 | +0.3333 |
| 意外 Action Macro 率 | 0.3333 | 0.0000 | -0.3333 |
| Top-1 根因命中率 | 1.0000 | 1.0000 | 0.0000 |
| 根因 TOOL 引用率 | 1.0000 | 1.0000 | 0.0000 |
| 历史案例投影通过率 | — | 1.0000 | — |
| 历史冲突保护通过率 | — | 1.0000 | — |
| Action 回归案例数 | — | 0 | — |
| 实时事实优先失败数 | — | 0 | — |

根因命中和引用率没有提升，是因为三条案例都预置了本次可审计 TOOL Evidence；这个零增益必须保留，
不能只展示 Action 覆盖的正增益。当前结果说明历史上下文在固定脚本中改善了一个动作选择，同时没有
破坏另外两例根因和引用安全。样本仅 3 条且 Planner/Auditor 为确定性替身，不能据此宣称真实模型
准确率普遍提升 33.33%，也不能推断线上时延、token 成本或故障恢复效果。

## 4. 可复现命令

```powershell
.venv\Scripts\python -m pytest -q `
  tests/unit/test_history_impact_evaluation.py `
  tests/integration/test_history_impact_langgraph.py
```

单元测试另行验证组件越界工具、off 组错误启用历史和冲突提示缺失；集成测试确认六次真实 LangGraph
运行各执行一次 ToolAction，off 组不搜索、on 组搜索一次，并把同一 confirmed 候选贯穿 Planner、
确定性 matcher、独立 Auditor 和最终结构化报告。
