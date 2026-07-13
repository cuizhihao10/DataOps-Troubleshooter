# GraphRAG 消融实测记录

本文只记录由当前仓库代码、合成知识种子和真实 PostgreSQL/pgvector 集成测试运行得到的数值，不把产品目标值或主观判断包装成实测成绩。

## 1. 实验条件

| 项目 | 实测配置 |
|---|---|
| 案例 | `ablation_sync_backlog_causal_chain` |
| 查询 | `sync backlog` |
| 知识数据 | `graph-seed:v4`，20 个节点、19 条边；旧知识保留 v1/v2/v3 source，新增检查点链使用 v4 source |
| Embedding Provider | `deterministic-hash:v1`，128 维 |
| 种子上限 | 5 |
| 图扩展上限 | 2 跳 |
| 对照组 | `vector_only`：相同 Provider 和 top-k，不执行全文查询，不扩图 |
| 实验组 | `vector_graph`：相同向量种子，沿批准关系扩展 1–2 跳 |
| 必要根因 | `root_cause_primary_key_conflict` |
| 必要路径 | `symptom_sync_backlog → root_cause_primary_key_conflict → solution_resolve_pk_conflict` |

## 2. 实测值

| 指标 | Vector-only | Vector+Graph | 差值 |
|---|---:|---:|---:|
| 根因节点命中 | 1 | 1 | 0 |
| 必要有序链路完整率 | 0.0 | 1.0 | +1.0 |

Vector-only 已把根因节点召回到 top-k，因此本案例不能宣称图提升了根因命中。图的实际贡献是返回 `path_4f6638ec28f7073d`，把症状、根因和方案通过 `CAUSED_BY → RESOLVED_BY` 两条真实边连接起来，使必要链路完整率从 0.0 提升到 1.0。

## 3. Evidence Bundle 实测

在默认 6000 字节、8 节点、4 路径预算下，Bundle 主体实测使用 4477 字节，选择 6 个去重节点和 4 条完整路径；另有 3 条路径因路径数预算被明确列入 `omitted_path_ids`。关键两跳因果路径完整进入 Bundle，没有截断节点正文或丢失任一条边。

这些数字只适用于上述固定数据、Provider 和代码。增加案例、替换 embedding 模型或修改预算后必须重新运行 PostgreSQL 集成测试并更新记录，不能将本次结果外推为通用准确率提升。

v4 新增知识没有改变 `sync backlog` 消融案例的 4477 字节 Bundle 实测值；同一 PostgreSQL 测试还会
分别查询 `LTS 参数校验失败 partition_date`、`BDS 执行阶段长尾 数据倾斜` 和
`FlashSync 检查点落后 位点回退`，要求递归 CTE 返回三条独立
“症状 → 根因 → 解决方案”路径。这些断言用于验证 v2/v3/v4 单组件知识真实入图，但没有被混入既有
主键冲突消融指标来制造额外增益。
