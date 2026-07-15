# 真实模型 Golden 评测状态与运行口径

## 当前状态

`live-golden-eval:v1` 的可执行基础已经完成，但本仓库当前**没有发布真实模型测量成绩**。原因不是
把失败隐藏为零分，而是项目禁止提交 API key，当前自动化验证也只使用官方 SDK 的 MockTransport。
在没有用户本地提供模型端点、密钥和 PostgreSQL 时，不得生成 `metric_kind=measured` 占位报告，
更不得把 28 条确定性 Golden runner 的满分宣传为真实 LLM 准确率。

这一状态文档与“实测结果文档”使用同一位置，是为了让作品集评审者明确看见：评测代码已经可运行，
真实成绩尚待在固定模型、Prompt、数据和代码版本下执行，而不是用省略表格暗示已经测量。

## v1 默认冒烟集合

默认集合固定三条案例，并保持以下顺序：

1. `golden_lts_invalid_partition_parameter_single`：单组件，要求日志正证与上游就绪反证同时存在。
2. `golden_cross_lts_bds_flashsync_watermark_timezone_mismatch`：三组件传播，要求 900 条缺口闭合。
3. `golden_bds_conflicting_partition_evidence`：三个工具均成功但事实冲突，要求无根因并人工复核。

这个三案例集合只用于低成本接线和安全冒烟，不代表 28 条完整真实模型成绩。显式传多个
`--case-id` 会生成 `scope=custom`，不能冒充标准 smoke；未来发布完整 28 条测量快照时应升级单独的
运行/结果契约，并记录所有类别分母。

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
  -> golden-diagnosis-eval:v21 scores the public DiagnosisRunResult
  -> live-golden-eval:v1 aggregates safe model-call telemetry
```

Live runner 不调用确定性 Golden runner，也不读取 Fixture 响应拼装答案。它只给 Planner 追加合成
`scenario_id`、资源 ID 和观察窗口，这些是 Mock MCP 的寻址字段；`required_tools`、允许根因、必要
Evidence source、故障路径、预期停止原因和风险答案不会进入模型消息。测试逐项断言这些标注没有泄漏。

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

- 不能宣称已有真实 LLM Top-1、Action 覆盖或 P95 成绩；当前没有测量快照。
- 不能把三案例 smoke 外推到 28 条或生产故障分布。
- 不能把 MockTransport 的固定 15 token 响应当作模型成本实测。
- 不能把确定性 Golden runner 的 100% 当作 Planner/Auditor 质量。
- 不能因为 token/耗时可观测就保存 Prompt、模型原始响应或 Thought。
