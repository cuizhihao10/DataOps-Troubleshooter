# 五层作品集评测统一实测报告

本文汇总 `portfolio-eval-manifest:v19` 当前登记的四层小样本消融和一层 Golden 确定性回归，并说明
`portfolio-eval-run:v19` 如何验证后再发布指标。它不是 28 条诊断 Golden Case 总成绩，也不计算跨层
“总准确率”；每个数字仍受各自数据、Provider、脚本和实验条件限制。代码可读取精确四层的历史 v1
Golden v1/v2/v3/v4/v5/v6/v7/v8/v9/v10/v11/v12/v13/v14/v15/v16/v17 来源的五层 v2/v3/v4/v5/v6/v7/v8/v9/v10/v11/v12/v13/v14/v15/v16/v17/v18 manifest，但默认 CLI 只运行五层 v19。

## 1. 统一执行范围

| Suite | 来源契约 | PostgreSQL | 详细实测文档 | 指标数 |
|---|---|---|---|---:|
| `graphrag_ablation` | `graphrag-retrieval:v2` | 必需 | `docs/graphrag-ablation-results.md` | 2 |
| `memory_recall_ablation` | `memory-recall-eval:v1` | 必需 | `docs/memory-recall-eval-results.md` | 2 |
| `history_impact_ablation` | `history-impact-eval:v1` | 不需要 | `docs/history-impact-eval-results.md` | 2 |
| `auditor_impact_ablation` | `auditor-impact-eval:v1` | 不需要 | `docs/auditor-impact-eval-results.md` | 3 |
| `golden_diagnosis_baseline` | `golden-diagnosis-eval:v18` | 不需要 | `docs/golden-diagnosis-eval-results.md` | 10 |

manifest 固定测试节点而不是自由命令。CLI 使用当前 Python 解释器和 `shell=False` 运行 pytest；只有
本次 status=`passed` 的 suite 才把相应 measured snapshot 放进 JSON。failed、skipped、blocked 的
`metrics` 必须为空，避免测试失败后仍展示旧成绩。

## 2. 当前统一指标快照

| 层 | 指标 | Control | Treatment | 差值 | 解释方向 |
|---|---|---:|---:|---:|---|
| GraphRAG | 根因节点命中 | 1.0000 | 1.0000 | 0.0000 | 根因基线已命中，诚实保留零增益 |
| GraphRAG | 必要有序链路完整率 | 0.0000 | 1.0000 | +1.0000 | 越高越好 |
| Memory Retrieval | Macro Recall@K | 0.8333 | 1.0000 | +0.1667 | 越高越好 |
| Memory Retrieval | Macro Precision@K | 0.8333 | 1.0000 | +0.1667 | 越高越好 |
| Memory Impact | 必要 Action Macro 覆盖率 | 0.6667 | 1.0000 | +0.3333 | 越高越好 |
| Memory Impact | 意外 Action Macro 率 | 0.3333 | 0.0000 | -0.3333 | 越低越好 |
| Auditor Impact | 预期问题 Macro 发现率 | 0.0000 | 1.0000 | +1.0000 | 越高越好 |
| Auditor Impact | 危险内容 Macro 残留率 | 1.0000 | 0.0000 | -1.0000 | 越低越好 |
| Auditor Impact | 安全处置率 | 0.0000 | 1.0000 | +1.0000 | 越高越好 |
| Golden Diagnosis | 28 条目标集覆盖率 | 1.0000 | 0.8929 | -0.1071 | 当前缺 3 条，未达发布资格 |
| Golden Diagnosis | 当前子集意图命中率 | 0.9000 | 1.0000 | +0.1000 | 25 条确定性脚本，不是模型准确率 |
| Golden Diagnosis | 当前有根因子集 Top-1 | 0.8000 | 1.0000 | +0.2000 | 18 条有根因案例 |
| Golden Diagnosis | 当前子集必要 Action 覆盖 | 0.9000 | 1.0000 | +0.1000 | Action 按标注确定性回放 |
| Golden Diagnosis | 当前有路径子集故障链完整率 | 0.8500 | 1.0000 | +0.1500 | 17 条案例、27 条路径，必须检索并报告 |
| Golden Diagnosis | 当前子集引用完整率 | 1.0000 | 1.0000 | 0.0000 | 只验证引用 ID 完整性 |
| Golden Diagnosis | 当前无根因子集安全降级 | 1.0000 | 1.0000 | 0.0000 | 7 条异常、空结果、冲突、补参、部分证据或全源不可用案例 |
| Golden Diagnosis | 记忆类别必要案例召回覆盖率 | 1.0000 | 1.0000 | 0.0000 | 3 条 confirmed memory 完整召回并投影 |
| Golden Diagnosis | 记忆类别实时事实优先通过率 | 1.0000 | 1.0000 | 0.0000 | 冲突旧根因未覆盖 TOOL Observation |
| Golden Diagnosis | 成功响应证据冲突安全处置率 | 1.0000 | 1.0000 | 0.0000 | 三个矛盾 Observation 均保留，禁止根因为零 |

这些 snapshot 只有对应 suite 本次通过时才出现在 CLI 输出。GraphRAG 和 Memory Retrieval 使用真实
PostgreSQL/pgvector；History/Auditor 使用真实 LangGraph 与确定性协议替身；Golden 使用顶层强类型
结果和 Fixture 回放脚本。它们共同证明工程边界可执行，但不能外推为真实 LLM 准确率、误报率、
P95、token 成本或生产恢复收益。

## 3. 完整与快速命令

完整运行：

```powershell
$env:DATAOPS_TEST_DATABASE_URL='postgresql+asyncpg://...'
.venv\Scripts\python -m app.evaluation
```

成功时五个 suite 均为 `passed`，共发布 19 个指标，并满足：

```json
{
  "contract_id": "portfolio-eval-run:v19",
  "metric_kind": "measured",
  "run_success": true,
  "complete": true,
  "all_suites_passed": true
}
```

无数据库快速模式：

```powershell
.venv\Scripts\python -m app.evaluation --skip-postgres
```

快速模式执行 History/Auditor/Golden 三层；两个 PostgreSQL suite 为 `skipped` 且不携带 metrics。即使已执行
层全部通过，报告仍为 `complete=false`、`all_suites_passed=false`，不能作为完整作品集成绩。

## 4. 尚未覆盖的目标

当前 `data/fixtures/golden_cases.json` 有 25 条案例、使用 15 个场景，而产品目标是 28 条完整诊断 Golden Cases。
Golden suite 明确发布 89.29% 覆盖率和 `target_coverage_complete=false`；统一运行器没有将 25 条基线或
四层消融案例相加后冒充 28 条。当前满分意图/Top-1 是确定性脚本数据流结果，尚未给出模型级准确率、
语义无依据结论率、排除故意异常后的 MCP 成功率、端到端 P95 或 token 成本。后续必须扩展 Golden
schema/案例和真实诊断 runner，固定模型/Prompt 配置后重新记录实测值。
