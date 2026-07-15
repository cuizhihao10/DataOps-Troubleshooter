# 长期记忆召回消融实测记录

本文只记录当前仓库、固定合成 corpus、确定性角度 Embedding Provider 和真实 PostgreSQL/pgvector
集成测试计算出的检索层指标。数字不代表 LLM 最终诊断准确率，也不能外推到生产数据或其他模型。

## 1. 评测契约与实验条件

| 项目 | 实测配置 |
|---|---|
| 契约 | `memory-recall-eval:v1` |
| 评测文件 | `data/evals/memory_recall_cases.json` |
| corpus | 5 条脱敏合成案例：4 confirmed、1 rejected |
| 查询案例 | 6 条：图救回、直接基线、rejected 隔离和 3 条直接命中回归 |
| Provider | `memory-recall-angle-eval:v1`，8 维单位方向 |
| 记忆去重阈值 | 0.99；本评测使用仓储直接建立独立 corpus，不运行语义去重 |
| 图关系阈值 | 0.8 |
| 对照组 | `vector_only`：相同 query、Provider 和 top-k，只执行 pgvector 直接召回 |
| 实验组 | `vector_graph`：直接 top-k 后沿 `case-memory-graph:v1` 的 `SIMILAR_TO` 出边扩展 |
| 图传播公式 | `seed_similarity * edge.weight` |
| 指标类型 | `measured`（实测值） |

角度设计只用于得到稳定、可人工复核的 cosine：查询为 0°，A/B/C 分别为 30°、315°、60°。
Vector-only top-2 是 A/B；A 与 C 的相似边权约 0.866，使 C 的图传播分约为
`0.866 × 0.866 = 0.75`，高于 B 的直接分约 0.707，因此 vector+graph 的最终 top-2 为 A/C。

## 2. 六条案例实测值

| 案例 | 模式 | 有序结果 | Recall@K | Precision@K | Forbidden hits |
|---|---|---|---:|---:|---:|
| `memory_recall_graph_rescue` | Vector-only | A, B | 0.5 | 0.5 | 0 |
| `memory_recall_graph_rescue` | Vector+Graph | A, C | 1.0 | 1.0 | 0 |
| `memory_recall_direct_baseline` | Vector-only | D | 1.0 | 1.0 | 0 |
| `memory_recall_direct_baseline` | Vector+Graph | D | 1.0 | 1.0 | 0 |
| `memory_recall_rejected_guard` | Vector-only | C | 1.0 | 1.0 | 0 |
| `memory_recall_rejected_guard` | Vector+Graph | C | 1.0 | 1.0 | 0 |
| `memory_recall_direct_case_a` | Vector-only / Vector+Graph | A / A | 1.0 | 1.0 | 0 |
| `memory_recall_direct_case_b` | Vector-only / Vector+Graph | B / B | 1.0 | 1.0 | 0 |
| `memory_recall_direct_case_c` | Vector-only / Vector+Graph | C / C | 1.0 | 1.0 | 0 |

图救回案例中的 C 只携带 `graph` 通道，不是直接 top-k 伪装的图命中；其稳定关系引用来自
`edge_case_similar_3bbf2feb0061bbe9`。数据库同时保存反向边
`edge_case_similar_421052dca8d95684`，因此从任一案例扩展都成立。

## 3. Macro 汇总实测值

| 指标 | Vector-only | Vector+Graph | 差值 |
|---|---:|---:|---:|
| Macro Recall@K | 0.9167 | 1.0000 | +0.0833 |
| Macro Precision@K | 0.9167 | 1.0000 | +0.0833 |
| Forbidden hit 总数 | 0 | 0 | 0 |
| Expected label 回归数 | — | 0 | 0 |

这里的增益只由 `memory_recall_graph_rescue` 一条固定几何案例贡献；其余五条用例证明图模式没有破坏
直接基线、直接命中回归，并且 rejected E 即使与撤销查询最相似也不会进入向量或图结果。样本仅 6 条，不能据此
宣称通用召回率提升 8.33%。替换 Provider、corpus、阈值、top-k 或评分公式后必须重新运行评测并
更新本文件。

## 4. 可复现命令

```powershell
$env:DATAOPS_TEST_DATABASE_URL='postgresql+asyncpg://...'
.venv\Scripts\python -m pytest -q tests/integration/test_memory_recall_evaluation_postgres.py -m postgres
```

单元测试还会验证 fixture 悬空引用、graph-only 计数、macro 公式，以及 vector-only 对照组若意外
携带 graph 通道必须失败，避免把未真正关闭图扩展的实验错误报告为图增益。
