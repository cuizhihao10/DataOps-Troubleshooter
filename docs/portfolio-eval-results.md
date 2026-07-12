# 四层作品集评测统一实测报告

本文汇总 `portfolio-eval-manifest:v1` 当前登记的四层小样本消融，并说明
`portfolio-eval-run:v1` 如何验证后再发布指标。它不是 28 条诊断 Golden Case 总成绩，也不计算跨层
“总准确率”；每个数字仍受各自数据、Provider、脚本和实验条件限制。

## 1. 统一执行范围

| Suite | 来源契约 | PostgreSQL | 详细实测文档 | 指标数 |
|---|---|---|---|---:|
| `graphrag_ablation` | `graphrag-retrieval:v2` | 必需 | `docs/graphrag-ablation-results.md` | 2 |
| `memory_recall_ablation` | `memory-recall-eval:v1` | 必需 | `docs/memory-recall-eval-results.md` | 2 |
| `history_impact_ablation` | `history-impact-eval:v1` | 不需要 | `docs/history-impact-eval-results.md` | 2 |
| `auditor_impact_ablation` | `auditor-impact-eval:v1` | 不需要 | `docs/auditor-impact-eval-results.md` | 3 |

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

这些 snapshot 只有对应 suite 本次通过时才出现在 CLI 输出。GraphRAG 和 Memory Retrieval 使用真实
PostgreSQL/pgvector；History/Auditor 使用真实 LangGraph 与确定性协议替身。它们共同证明工程边界
可执行，但不能外推为真实 LLM 准确率、误报率、P95、token 成本或生产恢复收益。

## 3. 完整与快速命令

完整运行：

```powershell
$env:DATAOPS_TEST_DATABASE_URL='postgresql+asyncpg://...'
.venv\Scripts\python -m app.evaluation
```

成功时四个 suite 均为 `passed`，共发布 9 个指标，并满足：

```json
{
  "contract_id": "portfolio-eval-run:v1",
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

快速模式只执行 History/Auditor 两层；两个 PostgreSQL suite 为 `skipped` 且不携带 metrics。即使已执行
层全部通过，报告仍为 `complete=false`、`all_suites_passed=false`，不能作为完整作品集成绩。

## 4. 尚未覆盖的目标

当前 `data/fixtures/golden_cases.json` 只有 5 条场景基线，而产品目标是 28 条完整诊断 Golden Cases。
统一运行器没有将 5 条格式基线或四层消融案例相加后冒充 28 条，也尚未给出模型级意图识别准确率、
根因 Top-1、无依据结论率、MCP 成功率、端到端 P95 或 token 成本。后续必须扩展真实诊断 runner、
固定模型/Prompt 配置并重新记录实测值。
