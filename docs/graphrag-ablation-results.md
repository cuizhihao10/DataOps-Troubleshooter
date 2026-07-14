# GraphRAG 消融实测记录

本文只记录由当前仓库代码、合成知识种子和真实 PostgreSQL/pgvector 集成测试运行得到的数值，不把产品目标值或主观判断包装成实测成绩。

## 1. 实验条件

| 项目 | 实测配置 |
|---|---|
| 案例 | `ablation_sync_backlog_causal_chain` |
| 查询 | `sync backlog` |
| 知识数据 | `graph-seed:v11`，54 个节点、71 条边；旧知识保留 v1–v10 source，新增订单履约链与水位线时区错配知识使用 v11 source |
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

在默认 6000 字节、8 节点、4 路径预算下，Bundle 主体实测使用 5634 字节，选择 7 个去重节点和 4 条完整路径；另有 5 个节点和 4 条路径因节点数、路径数或字节预算被明确列入 omitted IDs。关键两跳主键冲突因果路径完整进入 Bundle，没有截断节点正文或丢失任一条边。

这些数字只适用于上述固定数据、Provider 和代码。增加案例、替换 embedding 模型或修改预算后必须重新运行 PostgreSQL 集成测试并更新记录，不能将本次结果外推为通用准确率提升。

v7 新增任务拓扑曾使 `sync backlog` 固定查询的 Bundle 从 v6 的 4962 字节/7 节点变为 5881 字节/8 节点；
v8 客户分群节点没有进入该固定查询。v9 新增的目标写入限流知识与“同步积压”语义相关，因此成为
同一查询的次级候选：原子路径选择重新组合为 5634 字节/7 节点/4 路径，并明确省略 5 个节点和 4 条
路径。v10 的结算授权链和 v11 的订单水位线链都没有改变该固定查询的候选截断结果，因此上述 5634/7/4 快照继续由 PostgreSQL
集成测试实测锁定，而不是未经验证地沿用。主键冲突必要路径仍完整保留，消融根因命中和链路完整率不变；
这说明预算快照会随候选集合变化，
不能只更新总节点数而沿用旧字节数。同一 PostgreSQL 测试还会
分别查询 `LTS 参数校验失败 partition_date`、`BDS 执行阶段长尾 数据倾斜` 和
`FlashSync 检查点落后 位点回退`、`FlashSync Schema 记录拒绝 字段映射滞后`，要求递归 CTE 返回四条独立
“症状 → 根因 → 解决方案”路径。v6 还分别以 `dws_customer_profile_daily` 和
`flashsync_customer_profile_delta` 查询任务依赖链及 Schema 症状入口；v7 再以
`bds_customer_status_snapshot_hourly` 和 `flashsync_customer_status_delta` 查询 BDS→FlashSync 交付链及
检查点症状入口。v8 进一步以 `dws_customer_segment_daily` 和 `bds_customer_segment_daily` 分别查询
LTS→BDS→数据集交付链及 BDS→长尾→倾斜入口。v9 再分别以 `dws_revenue_dashboard_daily`、
`flashsync_payment_delta` 和 `TARGET_WRITE_THROTTLED` 查询三组件任务依赖、限流症状入口与受控恢复链。
v10 再以 `dws_settlement_summary_daily`、`flashsync_settlement_delta` 和
`SOURCE_AUTHORIZATION_EXPIRED` 查询结算任务依赖、源端授权拒绝症状入口与安全轮换方案链。
v11 最后以 `dws_order_fulfillment_daily`、`flashsync_order_event_delta` 和
`FlashSync 增量窗口静默漏数` 查询订单履约任务依赖、水位线症状入口与受控回补方案链。
这些断言验证 v2–v11 知识真实入图；新增候选可以诚实改变 Bundle 组成，但没有被包装成既有主键冲突
消融指标的额外增益。
