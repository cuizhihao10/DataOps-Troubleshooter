"""强制执行学习/求职项目的注释与实现原理文档约束。

本测试扫描所有人工编写的 Python 模块，要求存在足够说明职责和设计意图的模块 docstring，
并检查实现指南覆盖当前核心技术。这样新增文件无法在缺少学习型说明时通过测试。
"""

import ast
import io
import tokenize
from pathlib import Path
from zipfile import ZipFile

PYTHON_ROOTS = (Path("app"), Path("mcp_server"), Path("tests"))
MIN_MODULE_DOCSTRING_LENGTH = 60
MIN_CALLABLE_DOCSTRING_LENGTH = 80
CRITICAL_INLINE_COMMENT_FILES = (
    Path("app/api/main.py"),
    Path("app/capabilities/registry.py"),
    Path("app/agents/chat.py"),
    Path("app/agents/planner_adapter.py"),
    Path("app/agents/prompting.py"),
    Path("app/agents/auditor_chat.py"),
    Path("app/agents/auditor_adapter.py"),
    Path("app/agents/auditor_prompting.py"),
    Path("app/orchestration/react_loop.py"),
    Path("app/orchestration/report_workflow.py"),
    Path("app/orchestration/diagnosis_workflow.py"),
    Path("app/orchestration/diagnosis_runtime.py"),
    Path("app/orchestration/diagnosis_worker.py"),
    Path("app/orchestration/auditor_evaluation.py"),
    Path("app/orchestration/history_evaluation.py"),
    Path("app/evaluation/portfolio.py"),
    Path("app/evaluation/live_golden.py"),
    Path("app/observability/model_calls.py"),
    Path("app/reporting/draft.py"),
    Path("app/reporting/policy.py"),
    Path("app/reporting/revision.py"),
    Path("app/mcp/client.py"),
    Path("app/mcp/executor.py"),
    Path("app/mcp/observation.py"),
    Path("app/persistence/database.py"),
    Path("app/persistence/migrations/env.py"),
    Path("app/persistence/migrations/versions/20260710_0001_knowledge_graph.py"),
    Path("app/persistence/seed.py"),
    Path("app/retrieval/repository.py"),
    Path("app/retrieval/embeddings.py"),
    Path("app/retrieval/reranker.py"),
    Path("app/retrieval/budget.py"),
    Path("app/retrieval/ablation.py"),
    Path("app/retrieval/service.py"),
    Path("app/retrieval/chunking.py"),
    Path("app/retrieval/document_repository.py"),
    Path("app/retrieval/document_service.py"),
    Path("app/persistence/migrations/versions/20260716_0008_documents.py"),
    Path("app/memory/service.py"),
    Path("app/memory/repository.py"),
    Path("app/memory/runtime.py"),
    Path("app/memory/graph_registration.py"),
    Path("app/memory/evaluation.py"),
    Path("app/persistence/migrations/versions/20260713_0003_case_memories.py"),
    Path("app/persistence/migrations/versions/20260715_0004_diagnosis_resources.py"),
    Path("app/persistence/migrations/versions/20260716_0005_diagnosis_worker.py"),
    Path("app/persistence/run_repository.py"),
    Path("app/observability/tracing.py"),
    Path("app/observability/metrics.py"),
    Path("app/persistence/migrations/versions/20260716_0009_run_trace_spans.py"),
    Path("app/api/security.py"),
    Path("app/api/streaming.py"),
    Path("app/core/http_identity.py"),
    Path("mcp_server/repository.py"),
)
REQUIRED_GUIDE_SECTIONS = (
    "Pydantic 契约",
    "Fixture 与 Golden Case",
    "MCP 真实协议边界",
    "FastAPI lifespan",
    "PostgreSQL、SQLAlchemy、Alembic 与 pgvector",
    "显式 GraphRAG 路径",
    "固定 runtime capability registry",
    "LangGraph 有界 ReAct",
    "OpenAI-compatible Planner Structured Outputs",
    "确定性报告草稿与 Auditor Structured Outputs",
    "受控长期案例记忆",
    "端到端诊断编排",
    "资源化诊断 API",
    "测试分层",
    "真实模型 Golden 冒烟评测",
    "必需单页 Demo",
    "文档 RAG：第二条知识通道",
    "持久化 per-run 调用链与运行时指标",
    "资源 API 鉴权、限流与降级边界",
    "运行状态 SSE 增量推流",
    "双 Agent 契约：否决权、返工预算与降级阶梯",
)


def test_every_python_module_has_a_learning_oriented_docstring() -> None:
    """验证每个 Python 模块都先解释文件职责、技术边界和学习入口。

    AST 读取真正的模块 docstring，而不是把任意散落字符串误判为说明；长度下限只承担
    防止空壳文档的机械门禁，具体内容质量仍由代码评审检查。
    """

    missing_or_short: list[str] = []
    for root in PYTHON_ROOTS:
        for path in root.rglob("*.py"):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            docstring = ast.get_docstring(tree, clean=True) or ""
            if len(docstring) < MIN_MODULE_DOCSTRING_LENGTH:
                missing_or_short.append(f"{path} ({len(docstring)} chars)")

    assert missing_or_short == [], (
        "Every Python file needs a module docstring explaining responsibility, "
        f"principle, and boundary: {missing_or_short}"
    )


def test_every_python_callable_has_a_detailed_docstring() -> None:
    """验证所有类、函数、异步函数、方法与测试函数都有独立解释性说明。

    递归 AST 遍历会覆盖类方法和嵌套函数，因而文件头不能替代 callable 说明；失败消息
    精确给出文件、行号、类型和名称，方便开发者直接定位需要补充的学习型文档。
    """

    missing_or_short: list[str] = []
    callable_nodes = (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)

    for root in PYTHON_ROOTS:
        for path in root.rglob("*.py"):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if not isinstance(node, callable_nodes):
                    continue
                docstring = ast.get_docstring(node, clean=True) or ""
                if len(docstring) < MIN_CALLABLE_DOCSTRING_LENGTH:
                    kind = type(node).__name__
                    missing_or_short.append(
                        f"{path}:{node.lineno} {kind} {node.name} ({len(docstring)} chars)"
                    )

    assert missing_or_short == [], (
        "Every class/function/method/test needs a callable-level docstring explaining "
        f"inputs, outputs, principle, boundaries, and failure behavior: {missing_or_short}"
    )


def test_complex_boundary_modules_contain_critical_step_comments() -> None:
    """验证高风险边界函数体中存在解释关键步骤的内联注释。

    tokenize 能区分真实注释与字符串中的井号。这里要求每个协议、数据库或生命周期模块
    至少有两条非 shebang 注释，确保实现者说明校验顺序、重试、事务或资源释放原理。
    """

    insufficient: list[str] = []
    for path in CRITICAL_INLINE_COMMENT_FILES:
        source = path.read_text(encoding="utf-8")
        comments = [
            token.string
            for token in tokenize.generate_tokens(io.StringIO(source).readline)
            if token.type == tokenize.COMMENT and not token.string.startswith("#!")
        ]
        if len(comments) < 2:
            insufficient.append(f"{path} ({len(comments)} inline comments)")

    assert insufficient == [], (
        "Complex boundary modules need critical-step comments explaining control flow: "
        f"{insufficient}"
    )


def test_implementation_guide_covers_current_technology_boundaries() -> None:
    """确认面试型实现指南覆盖当前已经落地的全部技术边界。

    该测试防止代码引入协议或基础设施后只留下局部注释；若新增核心技术，开发者必须把
    原理、调用链、限制和验证方式同步到可连续阅读的实现指南。
    """

    guide = Path("docs/implementation-guide.md").read_text(encoding="utf-8")

    for section in REQUIRED_GUIDE_SECTIONS:
        assert section in guide
    assert "原始人工知识 JSON 的 embedding 仍为 `null`" in guide
    assert "可替换 Embedding Provider" in guide
    assert "五项混合评分" in guide
    assert "Evidence Bundle 上下文预算" in guide
    assert "Vector-only / Vector+Graph 消融" in guide
    assert "长期记忆召回消融评测" in guide
    assert "memory-recall-eval:v1" in guide
    assert "历史案例端到端影响消融评测" in guide
    assert "history-impact-eval:v1" in guide
    assert "独立 Auditor 增量影响消融评测" in guide
    assert "auditor-impact-eval:v1" in guide
    assert "golden-case:v7" in guide
    assert "golden-diagnosis-eval:v22" in guide
    # v22 的唯一行为差异是引用判定拆成悬空/实时支撑两条独立规则，文档必须显式记录，
    # 否则口径会在下一次改动里悄悄漂回 v21。
    assert "悬空判定直接调用报告层" in guide
    assert "GoldenEvidenceConflictExpectation" in guide
    assert "统一作品集评测 manifest 与单命令运行器" in guide
    assert "portfolio-eval-run:v22" in guide
    assert "live-golden-eval:v1" in guide
    assert "model-call-metric:v1" in guide
    assert "ContextVar" in guide
    assert "docs/frontend-design.md" in guide
    assert "graph-seed:v11" in guide
    assert "54 节点/71 边" in guide
    assert "authorization_value_exposed=false" in guide
    assert "WATERMARK_TIMEZONE_MISMATCH" in guide
    assert "明确保留为接入点" in guide
    assert "代码注释的强制粒度" in guide
    assert "callable 级 docstring" in guide


def test_observability_documents_pin_trace_contract_and_no_reasoning_leak_boundary() -> None:
    """确认调用链与指标文档锁定两个契约 ID、七层 span、两个读取入口和不外泄推理的结构性理由。

    可观测性最容易退化成“加了埋点就算完事”：一旦文档不写清楚属性值被限制为 ASCII 才使得
    Prompt/Thought 无法进入 trace，后续维护者会顺手加一个自由文本字段而破坏这条保证。同时锁定
    “未装配返回 503 而不是全零曝光”，防止把“没部署”渲染成“零错误”这类最危险的监控假象。
    """

    guide = Path("docs/implementation-guide.md").read_text(encoding="utf-8")
    contracts = Path("docs/prompt-contracts.md").read_text(encoding="utf-8")
    readme = Path("README.md").read_text(encoding="utf-8")

    assert "run-trace:v1" in guide
    assert "runtime-metrics:v1" in guide
    assert "workflow / node / react_step / tool_call / retrieval / model_call /" in guide
    assert "GET /api/v1/runs/{run_id}/trace" in guide
    assert "dataops_span_error_count{kind,name}" in guide
    assert "MAX_SPANS_PER_RUN = 512" in guide
    assert "dropped_span_count" in guide
    assert "在类型层面就无法" in guide
    assert "全零会被看板渲染成“零错误”" in guide
    assert "run-trace:v1" in contracts
    assert "runtime-metrics:v1" in contracts
    assert "run-trace:v1" in readme
    assert "runtime-metrics:v1" in readme


def test_api_auth_documents_pin_failclosed_scope_and_indistinguishable_rejection() -> None:
    """确认鉴权文档锁定前缀 fail-closed 范围、限流先于鉴权的顺序和不可区分的 401 边界。

    鉴权最容易退化成"加了个令牌就算有安全边界"：只要文档不写清判定顺序，后续维护者会顺手把
    401 提前到限流之前，令牌猜测就重新变成免费操作；只要文档不写清三种失败必须逐字相同，
    有人会为了"更友好的报错"把"缺令牌"和"令牌错误"分开，从而泄露该实例是否已配置令牌。
    """

    guide = Path("docs/implementation-guide.md").read_text(encoding="utf-8")
    contracts = Path("docs/prompt-contracts.md").read_text(encoding="utf-8")
    design = Path("docs/product-design.md").read_text(encoding="utf-8")
    readme = Path("README.md").read_text(encoding="utf-8")

    assert "api-auth:v1" in guide
    assert "api-auth:v1" in contracts
    assert "api-auth:v1" in design
    assert "api-auth:v1" in readme
    assert '("/api/v1", "/metrics")' in contracts
    assert "先限流再鉴权" in contracts
    assert 'WWW-Authenticate: Bearer realm="dataops-api"' in contracts
    assert "MINIMUM_API_TOKEN_CHARS = 32" in contracts
    assert "hmac.compare_digest" in contracts
    assert "fail closed" in contracts
    assert "9.4 资源 API 鉴权与限流" in design
    assert "127.0.0.1" in design


def test_run_stream_documents_pin_read_only_boundary_and_end_reason_taxonomy() -> None:
    """确认推流文档锁定"只读投递、顺序不变量、终止原因分类"和轮询永久回退四条边界。

    推流最容易退化成"顺手让长连接也能推进 run"：只要文档不写清生成器仅持有两个只读方法，
    后续维护者会把重试或补偿逻辑塞进流里，于是断流开始改变诊断结论。同样地，只要不写清
    "先读快照再读增量事件"，有人会为了少一次轮询把顺序反过来，`stream_end` 就会发在时间线
    缺最后几条事件的位置上；而把 run 级终态与连接级超时混成一个原因，前端只能二选一地犯错。
    """

    guide = Path("docs/implementation-guide.md").read_text(encoding="utf-8")
    contracts = Path("docs/prompt-contracts.md").read_text(encoding="utf-8")
    design = Path("docs/product-design.md").read_text(encoding="utf-8")
    readme = Path("README.md").read_text(encoding="utf-8")

    assert "run-stream:v1" in guide
    assert "run-stream:v1" in contracts
    assert "run-stream:v1" in design
    assert "run-stream:v1" in readme
    assert "GET /api/v1/runs/{run_id}/stream" in guide
    assert "先读 run 快照 → 再读增量事件" in guide
    assert "run_snapshot" in contracts
    assert "stream_end" in contracts
    assert "run_disappeared" in contracts
    assert "stream_timeout" in contracts
    assert "Last-Event-ID" in contracts
    assert "不是第二条执行路径" in contracts
    assert "run_stream_max_seconds > react_total_timeout_seconds" in contracts
    assert "available_under_auth" in design
    assert "轮询是永久保留的等价通道" in design


def test_dual_agent_documents_pin_veto_asymmetry_revision_budget_and_degrade_ladder() -> None:
    """确认双 Agent 文档锁定四级降级阶梯、规则对模型的非对称否决权和四个出口的暴露粒度。

    审计最容易退化成"装饰性审计"：只要文档不写清确定性问题非空就强制 revise，后续维护者会让
    模型的 accept 直接放行；只要不写清"Auditor 不可用不消耗返工预算且不等于通过"，有人会把
    Provider 故障当成 accept。同时锁定"前端只显示问题码与字段路径"，防止未经放行的模型措辞
    以"审计意见"的形式回到页面上。
    """

    guide = Path("docs/implementation-guide.md").read_text(encoding="utf-8")

    assert "双 Agent 契约：否决权、返工预算与降级阶梯" in guide
    assert "auditor_unavailable" in guide
    assert "任何规则问题非空即强制 `revise`" in guide
    assert "放行条件是**两层都同意**" in guide
    assert "不消耗返工预算" in guide
    assert '"审计不可用"绝不等于"审计通过"' in guide
    assert "降级" in guide
    assert "永远不会进入长期记忆" in guide
    assert "只删不加" in guide
    assert "从不读 `revision_instructions`" in guide
    assert "未经放行的表述不应以" in guide
    assert "不能外推为真实 LLM 的审计质量" in guide


def test_live_golden_status_document_separates_runnable_contract_from_measurement() -> None:
    """确认真实模型评测文档标明 smoke 范围、答案隔离、安全 token 遥测与未达标项。

    该门禁防止作品集把三案例 smoke 包装成完整真实模型成绩；文档必须保留默认三案例、生产运行路径、
    measured-only、usage 缺失、不保存 Prompt/Thought，以及"未达成 P95 目标"和评分口径缺陷的自述。
    """

    report = Path("docs/live-golden-eval-results.md").read_text(encoding="utf-8")

    assert "live-golden-eval:v1" in report
    assert "只发布三案例 smoke 的实测成绩" in report
    assert "planner-react:v8" in report
    assert "golden_lts_invalid_partition_parameter_single" in report
    assert "golden_cross_lts_bds_flashsync_watermark_timezone_mismatch" in report
    assert "golden_bds_conflicting_partition_evidence" in report
    assert "不读取 Fixture 响应拼装答案" in report
    assert "ContextVar" in report
    assert "unreported_usage_call_count" in report
    assert "Prompt、模型原始响应或 Thought" in report
    assert "不能把三案例 smoke 外推到 28 条" in report
    # 未达标项与口径缺陷必须留在文档里：删掉它们等于把设计目标当成实测成绩对外宣称。
    assert "不能宣称达成 P95 ≤ 30 s" in report
    assert "评分口径缺陷而非报告质量下降" in report



def test_frontend_design_is_mandatory_and_defines_safe_async_demo_acceptance() -> None:
    """确认前端没有从范围中遗失，并固定异步状态、证据展示与安全验收边界。

    文档必须把单页 Demo 标为必需但尚未完成，说明为何等待 Worker 契约，并锁定 run/event 轮询、
    Evidence/GraphRAG/报告/记忆展示、JSDoc、XSS 和不展示 Thought 的完成定义。
    """

    design = Path("docs/frontend-design.md").read_text(encoding="utf-8")

    assert "必需交付项" in design
    assert "当前仓库已经包含同源静态 Demo" in design
    assert "queued -> running -> completed|failed|cancelled" in design
    assert "FastAPI 静态托管" in design
    assert "Action/Observation 时间线" in design
    assert "GraphRAG 路径" in design
    assert "confirm/reject" in design
    assert "详细 JSDoc" in design
    assert "不用不可信数据赋值 `innerHTML`" in design
    assert "不提供 Thought 展开按钮" in design


def test_memory_recall_measured_report_documents_scope_and_no_generalized_claim() -> None:
    """确认长期记忆实测报告标明固定条件、实测属性、禁止命中和不可外推边界。

    该测试防止 README/作品集只保留漂亮增益数字却删除小样本限制；报告必须记录版本化契约、两种
    模式、Macro 指标、forbidden hit 和“不能外推”声明，缺任一项都应阻止合并。
    """

    report = Path("docs/memory-recall-eval-results.md").read_text(encoding="utf-8")

    assert "memory-recall-eval:v1" in report
    assert "Vector-only" in report
    assert "Vector+Graph" in report
    assert "Macro Recall@K" in report
    assert "Macro Precision@K" in report
    assert "Forbidden hit" in report
    assert "不能外推" in report


def test_history_impact_report_documents_measured_scope_and_realtime_priority() -> None:
    """确认端到端历史影响报告同时记录行为增益、零根因增益和不可外推边界。

    该门禁防止作品集只展示必要 Action 覆盖提高，却删除根因命中持平、确定性替身、小样本和实时
    事实优先限制；报告还必须说明 off/on、ToolEvent、历史投影和冲突保护的具体实测口径。
    """

    report = Path("docs/history-impact-eval-results.md").read_text(encoding="utf-8")

    assert "history-impact-eval:v1" in report
    assert "Memory off" in report
    assert "Memory on" in report
    assert "必要 Action Macro 覆盖率 | 0.6667 | 1.0000 | +0.3333" in report
    assert "Top-1 根因命中率 | 1.0000 | 1.0000 | 0.0000" in report
    assert "根因 TOOL 引用率 | 1.0000 | 1.0000 | 0.0000" in report
    assert "历史冲突保护通过率" in report
    assert "确定性替身" in report
    assert "不能据此宣称真实模型" in report


def test_auditor_impact_report_separates_rules_control_and_model_quality_claims() -> None:
    """确认 Auditor 消融报告区分规则对照、独立审计、降级和真实模型质量边界。

    该门禁防止 README/报告把 off 组写成生产开关、把 degraded 写成接受，或把确定性脚本小样本的
    100% 发现率宣传为真实 LLM 准确率；同时锁定危险残留和安全处置两个最终结果指标。
    """

    report = Path("docs/auditor-impact-eval-results.md").read_text(encoding="utf-8")

    assert "auditor-impact-eval:v1" in report
    assert "Auditor off" in report
    assert "control_unreviewed" in report
    assert "不是生产功能开关" in report
    assert "预期问题 Macro 发现率 | 0.0000 | 1.0000 | +1.0000" in report
    assert "危险内容 Macro 残留率 | 1.0000 | 0.0000 | -1.0000" in report
    assert "安全处置率 | 0.0000 | 1.0000 | +1.0000" in report
    assert "持续问题后安全降级 | 1" in report
    assert "不能据此宣称真实 LLM" in report


def test_portfolio_eval_report_documents_publish_gate_and_complete_golden_scope() -> None:
    """确认统一报告锁定 passed 才发布指标、快速模式不完整和 28/28 数量边界。

    该门禁防止 CLI 失败后仍宣传 manifest 快照，也防止把 28 条确定性脚本数量达标冒充真实模型
    诊断成绩；完整命令、快速命令和十九个指标范围都必须可见。
    """

    report = Path("docs/portfolio-eval-results.md").read_text(encoding="utf-8")

    assert "portfolio-eval-manifest:v23" in report
    assert "portfolio-eval-run:v22" in report
    assert "只有" in report and "status=`passed`" in report
    assert "failed、skipped、blocked" in report
    assert "python -m app.evaluation" in report
    assert "--skip-postgres" in report
    assert '"complete": true' in report
    assert "共发布 19 个指标" in report
    assert "有 28 条案例、使用 18 个场景" in report
    assert "成功响应证据冲突安全处置率" in report
    assert "28 条产品目标" in report
    assert "不能外推为真实 LLM" in report


def test_golden_diagnosis_report_documents_scoring_and_twenty_eight_case_boundary() -> None:
    """确认 Golden 报告解释水位线三组件传播、安全边界和 28/28 完整资格。

    该门禁防止把脚本按标注选择 Action/根因得到的满分宣传为模型准确率；报告还必须区分 Top-1
    有根因分母、安全降级分母、结构引用检查和故意异常工具成功率。水位线案例还必须证明进程完成
    不等于数据正确，并保留禁止自动修改和生产写入的高风险边界。
    """

    report = Path("docs/golden-diagnosis-eval-results.md").read_text(encoding="utf-8")

    assert "golden-case:v7" in report
    assert "golden-diagnosis-eval:v22" in report
    assert "28/28 = 100%" in report
    assert "target_coverage_complete=true" in report
    assert "确定性脚本" in report
    assert "不能外推为真实 LLM" in report
    assert "根因 Top-1" in report
    assert "故障链路完整率 | 100%" in report
    assert "RetrievedPath" in report
    assert "安全降级率" in report
    assert "关键结论引用完整率" in report
    assert "单组件明确故障 | 8 | 8 | 0" in report
    assert "长期记忆召回 | 3 | 3 | 0" in report
    assert "工具尝试成功率 | 92.93%" in report
    assert "必要历史召回覆盖率 | 100%" in report
    assert "实时事实优先通过率 | 100%" in report
    assert "禁止记忆命中数 | 0" in report
    assert "证据冲突安全处置率 | 100%" in report
    assert "禁止冲突根因命中数 | 0" in report
    assert "跨组件故障 | 10 | 10 | 0" in report
    assert "20 条适用案例、36 条必要路径" in report
    assert "cross_lts_bds_resource_exhaustion" in report
    assert "零 MCP Action" in report
    assert "missing_resource_id" in report
    assert "flashsync_incomplete_root_cause_evidence" in report
    assert "EMPTY_RESULT" in report
    assert "状态、日志和依赖拓扑" in report
    assert "attempt 1/2" in report
    assert "lts_parameter_validation_failure" in report
    assert "INVALID_PARTITION_DATE" in report
    assert "graph-seed:v11" in report
    assert "bds_data_skew" in report
    assert "DATA_SKEW_DETECTED" in report
    assert "9.6 倍热点分桶" in report
    assert "flashsync_checkpoint_regression" in report
    assert "CHECKPOINT_REGRESSION" in report
    assert "风险必须命中 high" in report
    assert "flashsync_schema_mapping_outdated" in report
    assert "SCHEMA_MAPPING_OUTDATED" in report
    assert "单组件 8/8" in report
    assert "cross_customer_profile_schema_propagation" in report
    assert "LTS→BDS→FlashSync" in report
    assert "5000 条预期记录只到达 4400 条" in report
    assert "cross_bds_flashsync_checkpoint_regression" in report
    assert "8000 条只到达 6800 条" in report
    assert "风险命中 high" in report
    assert "cross_lts_bds_data_skew" in report
    assert "synthetic_segment_unknown" in report
    assert "318 万行位于 300–340 万基线" in report
    assert "cross_lts_bds_flashsync_target_throttle" in report
    assert "TARGET_WRITE_THROTTLED" in report
    assert "吞吐从 450 降至 8 行/秒" in report
    assert "不自动提额或写入目标端" in report
    assert "cross_lts_bds_flashsync_source_auth_expired" in report
    assert "SOURCE_AUTHORIZATION_EXPIRED" in report
    assert "authorization_value_exposed=false" in report
    assert "不展示、" in report and "任何授权值" in report
    assert "风险命中 high" in report
    assert "cross_lts_bds_flashsync_watermark_timezone_mismatch" in report
    assert "WATERMARK_TIMEZONE_MISMATCH" in report
    assert "UTC" in report and "Asia/Shanghai" in report
    assert "偏移 480 分钟" in report
    assert "缺失 900 条且零重复" in report
    assert "不自动修改水位线" in report


def test_prompt_contract_versions_budgeted_retrieval_inputs() -> None:
    """确认 Prompt 契约区分完整检索 v3、预算化 Evidence Bundle v2 与文档检索 v1 输入。

    Planner Prompt 文本未改变，但三个占位符的数据语义已经版本化；测试防止后续代码升级 contract ID
    后文档仍声称旧结构，或遗漏路径原子选择、文档切片预算和 truncated 的解释。
    """

    prompt_contract = Path("docs/prompt-contracts.md").read_text(encoding="utf-8")

    assert "graphrag-retrieval:v3" in prompt_contract
    assert "graphrag-evidence-bundle:v2" in prompt_contract
    assert "document-retrieval:v1" in prompt_contract
    assert "路径只有在其全部节点、边和来源能一起进入预算" in prompt_contract
    assert "truncated=true" in prompt_contract
    assert "omitted_chunk_ids" in prompt_contract
    assert '"semantic": 0.60, "lexical": 0.25, "authority": 0.15' in prompt_contract


def test_document_rag_channel_is_documented_with_honesty_boundaries() -> None:
    """确认实现指南把文档 RAG 讲成独立通道，并保留切片、评分与建议提升的边界说明。

    文档通道最容易退化成"把整份 Runbook 塞进 Prompt"，因此测试锁定三条关键约束：切片是唯一
    引用单元、评分刻意只用三因子、只有 Runbook/SOP 的步骤小节能变成处置建议。任何一条被删除
    都说明实现或文档已经偏离产品设计，而不是单纯的措辞调整。
    """

    guide = Path("docs/implementation-guide.md").read_text(encoding="utf-8")

    assert "文档 RAG：第二条知识通道" in guide
    assert "切片而不是文档是检索与引用单元" in guide
    assert "三因子评分，刻意不复用图侧的五因子" in guide
    assert "只有 Runbook/SOP 的步骤小节能变成处置建议" in guide
    assert "document_chunks_embedded != document_chunks_loaded" in guide
    assert "6000 个 UTF-8 JSON 字节、8 个唯一节点、4 条路径和 3 个文档切片" in guide


def test_runtime_capability_prompt_contract_is_versioned_and_bounded() -> None:
    """确认 Planner capability 上下文拥有版本、固定集合和历史按需触发边界。

    测试锁定的是数据契约与安全原则而非整段自然语言，防止后续删除固定 registry、把历史召回
    变成每轮默认步骤，或允许 capability 自行调用 LLM/MCP 却未提升版本和更新设计。
    """

    prompt_contract = Path("docs/prompt-contracts.md").read_text(encoding="utf-8")

    assert "runtime-capabilities:v1" in prompt_contract
    assert "单组件诊断、跨组件链路溯源、历史案例匹配、风险评估和结构化报告" in prompt_contract
    assert (
        "not_requested | user_requested | planner_validation | reusable_signature"
        in prompt_contract
    )
    assert "registry 不解析自然语言，也不调用 LLM、MCP、检索或记忆服务" in prompt_contract
    assert "实时 Observation 都高于案例和知识证据" in prompt_contract


def test_langgraph_react_runtime_contract_is_versioned_and_explicit() -> None:
    """确认 Prompt 文档记录真实 LangGraph 循环、停止原因、Action 计数口径和并行批次语义。

    该门禁防止实现退化为手写单轮函数，或把 MCP 内部重试误算成 Planner 步数；同时要求文档
    明确模型适配器仍是独立边界，不能把测试 Scripted Planner 宣传成生产 Agent。并行相关断言
    锁定"一批 N 个 Action 记 N 步"与"整批拒绝不截断"两条决定，避免文档把并行说成额外取证预算。
    """

    prompt_contract = Path("docs/prompt-contracts.md").read_text(encoding="utf-8")

    assert "langgraph-react-loop:v3" in prompt_contract
    assert "Planner → execute_tools → Observation → Planner" in prompt_contract
    assert "MCP 执行器内部的瞬时重试不增加 `react_step`" in prompt_contract
    assert "一批 N 个 Action 记 N 步" in prompt_contract
    assert "任一门禁不通过都整批拒绝而不截断执行" in prompt_contract
    assert "duplicate_action_blocked" in prompt_contract
    assert "parallel_limit_exceeded" in prompt_contract
    assert "parallel_budget_exceeded" in prompt_contract
    assert "react.tool_batch" in prompt_contract
    assert "planner_provider_error" in prompt_contract
    assert "planner_refusal" in prompt_contract
    assert "planner_output_invalid" in prompt_contract


def test_planner_v5_and_structured_output_repair_are_documented() -> None:
    """确认 Prompt 契约记录 v5 会话/历史上下文、并行批次上限、角色隔离和一次修复语义。

    测试锁定安全与可复现边界而非自然语言全文：用户数据不能进入 system，SDK 不发送 API tools，
    refusal 不修复，第二次失败停止；同时保留官方文档链接便于学习者核对原理。批次上限断言额外
    钉住"strict Schema 不接受 maxItems"这条实现约束，防止有人把校验挪回 Field 后文档失真。
    """

    prompt_contract = Path("docs/prompt-contracts.md").read_text(encoding="utf-8")

    assert "planner-react:v8" in prompt_contract
    assert "session_context" in prompt_contract
    assert "history_case_matches" in prompt_contract
    assert "system/user 两条消息" in prompt_contract
    assert "{remaining_tool_calls}" in prompt_contract
    assert "{max_parallel_actions}" in prompt_contract
    assert "{unexecuted_priority_tools}" in prompt_contract
    assert "strict Schema 不接受" in prompt_contract
    assert "openai-compatible-planner:v1" in prompt_contract
    assert "chat.completions.parse(response_format=PlannerDecision)" in prompt_contract
    assert "Provider 不传 `tools` 或 `tool_choice`" in prompt_contract
    assert "第二次仍无效" in prompt_contract
    assert "refusal" in prompt_contract
    assert "https://developers.openai.com/api/docs/guides/structured-outputs" in prompt_contract


def test_auditor_prompt_and_bounded_revision_contract_are_documented() -> None:
    """确认独立 Auditor Prompt、规则否决和一次报告返工拥有版本化契约。

    门禁要求 Auditor 使用 Structured Outputs、不注册 API tools，确定性问题可否决 accept，二次
    revise 或 Provider 失败返回 degraded；这防止实现退化成无约束的同模型自评。
    """

    prompt_contract = Path("docs/prompt-contracts.md").read_text(encoding="utf-8")

    assert "auditor-report:v2" in prompt_contract
    assert "openai-compatible-auditor:v1" in prompt_contract
    assert "chat.completions.parse(response_format=AuditResult)" in prompt_contract
    assert "确定性问题拥有最终否决权" in prompt_contract
    assert "audited-report-workflow:v2" in prompt_contract
    assert "最多一次报告级返工" in prompt_contract
    assert "安全降级报告" in prompt_contract
    assert "auditor-impact-eval:v1" in prompt_contract
    assert "control_unreviewed" in prompt_contract
    assert "确定性预检 `AuditIssue` 完全相同且为空" in prompt_contract
    assert "不为生产运行时增加关闭开关" in prompt_contract


def test_case_memory_contract_is_versioned_audited_and_confirmed_only() -> None:
    """确认长期案例记忆文档锁定审计门禁、两阶段去重、来源幂等和默认可见性边界。

    该测试读取 Prompt/运行契约而不是实现源码，防止后续重构在代码仍能运行时悄悄把 pending 候选
    暴露给 Planner，或遗漏 ``memory_evidence`` 对同 run 重放和证据审计的约束。缺少任一关键术语
    都表示学习文档没有与 ``case-memory:v2`` 实现同步，应在合并前失败。
    """

    prompt_contract = Path("docs/prompt-contracts.md").read_text(encoding="utf-8")

    assert "case-memory:v2" in prompt_contract
    assert "pending" in prompt_contract
    assert "exact signature" in prompt_contract
    assert "pgvector cosine" in prompt_contract
    assert "same run idempotency" in prompt_contract
    assert "confirmed-only" in prompt_contract
    assert "memory_evidence" in prompt_contract
    assert "GraphRAG `case` 节点" in prompt_contract
    assert "双向" in prompt_contract
    assert "图同步失败必须回滚状态" in prompt_contract
    assert "seed_similarity * edge.weight" in prompt_contract
    assert "graph_edge_refs" in prompt_contract


def test_top_level_diagnosis_workflow_contract_orders_recall_audit_and_staging() -> None:
    """确认顶层诊断契约锁定按需历史召回、双子图复用和审计后 staging 顺序。

    该测试防止后续 API 接线绕过顶层 workflow，或把案例查询变成每轮默认动作。文档必须说明
    ``recall_case_memories`` 只在 trigger 后执行、同一批 confirmed 案例进入 Planner/Auditor，且
    ``stage_case_memory`` 位于报告审计之后；缺少任一边界都应阻止合并。
    """

    prompt_contract = Path("docs/prompt-contracts.md").read_text(encoding="utf-8")

    assert "audited-diagnosis-workflow:v2" in prompt_contract
    assert "recall_case_memories" in prompt_contract
    assert "explain_case_matches" in prompt_contract
    assert "共同点" in prompt_contract
    assert "避坑提示" in prompt_contract
    assert "不重新搜索" in prompt_contract
    assert "history_trigger=not_requested" in prompt_contract
    assert "Planner 与 Auditor" in prompt_contract
    assert "stage_case_memory" in prompt_contract
    assert "skipped_not_accepted" in prompt_contract
    assert "history-impact-eval:v1" in prompt_contract
    assert "memory_off" in prompt_contract
    assert "memory_on" in prompt_contract
    assert "ToolEvent" in prompt_contract
    assert "不能单独" in prompt_contract


def test_diagnosis_resource_contract_documents_persistence_events_and_failure_semantics() -> None:
    """确认资源 API 契约记录同步执行、四表/checkpoint、公开事件和安全失败查询方式。

    该门禁防止后续把进程内 background task 宣称为可靠队列，或让 run_events 保存 Thought/原始异常。
    文档必须保留 `diagnosis-resources:v2`、running/completed/failed 状态和失败后凭 run_id 查询语义。
    """

    prompt_contract = Path("docs/prompt-contracts.md").read_text(encoding="utf-8")

    assert "diagnosis-resources:v4" in prompt_contract
    assert "diagnosis_sessions" in prompt_contract
    assert "agent_runs" in prompt_contract
    assert "run_events" in prompt_contract
    assert "session_checkpoints" in prompt_contract
    assert "session-checkpoint:v1" in prompt_contract
    assert "失败或取消的 run 不覆盖旧快照" in prompt_contract
    assert "postgres-worker" in prompt_contract
    assert "queued | running | completed | failed" in prompt_contract
    assert "FOR UPDATE SKIP LOCKED" in prompt_contract
    assert "HTTP 409" in prompt_contract
    assert "不保存 Thought" in prompt_contract
    assert "portfolio-eval-manifest:v23" in prompt_contract
    assert "实时支撑判定" in prompt_contract
    assert "portfolio-eval-run:v22" in prompt_contract
    assert "subprocess.run(shell=False)" in prompt_contract
    assert "failed、skipped 或 blocked 必须隐藏 metrics" in prompt_contract
    assert "28/28 条和五类配额已经完整" in prompt_contract
    assert "required_fault_paths" in prompt_contract
    assert "case_category" in prompt_contract
    assert "至少包含两个不同组件前缀" in prompt_contract
    assert "history_expectation" in prompt_contract
    assert "evidence_conflict_expectation" in prompt_contract
    assert "零 `required_tools`" in prompt_contract
    assert "TOOL Evidence" in prompt_contract
    assert "检索到但未写入报告" in prompt_contract


def test_ablation_report_labels_measured_values_and_honest_zero_gain() -> None:
    """确认消融报告明确标注实测条件，并保留根因命中没有提升的真实结果。

    该门禁防止作品集只展示链路正增益却删除根因差值为零的事实，也要求报告记录 Provider、预算、
    path_id 和适用范围，避免把单个合成案例外推为通用准确率。
    """

    report = Path("docs/graphrag-ablation-results.md").read_text(encoding="utf-8")

    assert "实测值" in report
    assert "deterministic-hash:v1" in report
    assert "根因节点命中 | 1 | 1 | 0" in report
    assert "必要有序链路完整率 | 0.0 | 1.0 | +1.0" in report
    assert "path_4f6638ec28f7073d" in report
    assert "graph-seed:v11" in report
    assert "54 个节点、71 条边" in report
    assert "5658 字节" in report
    assert "7 个去重节点" in report
    assert "5 个节点和 4 条路径" in report
    assert "LTS 参数校验失败 partition_date" in report
    assert "BDS 执行阶段长尾 数据倾斜" in report
    assert "FlashSync 检查点落后 位点回退" in report
    assert "FlashSync Schema 记录拒绝 字段映射滞后" in report
    assert "dws_customer_profile_daily" in report
    assert "flashsync_customer_profile_delta" in report
    assert "bds_customer_status_snapshot_hourly" in report
    assert "flashsync_customer_status_delta" in report
    assert "dws_customer_segment_daily" in report
    assert "bds_customer_segment_daily" in report
    assert "dws_revenue_dashboard_daily" in report
    assert "flashsync_payment_delta" in report
    assert "TARGET_WRITE_THROTTLED" in report
    assert "dws_settlement_summary_daily" in report
    assert "flashsync_settlement_delta" in report
    assert "SOURCE_AUTHORIZATION_EXPIRED" in report
    assert "dws_order_fulfillment_daily" in report
    assert "flashsync_order_event_delta" in report
    assert "FlashSync 增量窗口静默漏数" in report
    assert "不能将本次结果外推" in report


def test_agents_policy_marks_documentation_as_definition_of_done() -> None:
    """确认仓库规则与开发 Skill 都把细粒度注释列入完成定义。

    同时检查两处规则是为了让人工开发和 Codex 辅助开发使用同一标准，避免只更新其中
    一个入口后，后续切片再次生成只有文件头说明的实现。
    """

    policy = Path("AGENTS.md").read_text(encoding="utf-8")
    skill = Path(".agents/skills/build-dataops-troubleshooter/SKILL.md").read_text(encoding="utf-8")

    assert "学习和求职展示" in policy
    assert "docs/implementation-guide.md" in policy
    assert "学习型说明是完成定义的一部分" in policy
    assert "每个类、函数、异步函数、方法和测试函数" in policy
    assert "文件开头的统一说明不能替代" in policy
    assert "保证学习与求职可解释性" in skill
    assert "将学习型说明纳入完成定义" in skill
    assert "callable 级 docstring" in skill
    assert "AST 扫描" in skill


def test_learning_constraint_is_synchronized_to_formal_docx() -> None:
    """确认正式 DOCX 阅读版包含与 Markdown 基线一致的学习型约束。

    DOCX 是面试展示用正式文档，无法通过普通文本 diff 可靠审查，因此测试直接读取压缩包
    内的 WordprocessingML，至少验证关键章节和实现指南引用没有在生成时丢失。
    """

    docx_path = next(Path("docs").glob("*.docx"))
    with ZipFile(docx_path) as package:
        document_xml = package.read("word/document.xml").decode("utf-8")

    assert "学习与求职可解释性" in document_xml
    assert "docs/implementation-guide.md" in document_xml
    assert "callable 级 docstring" in document_xml
    assert "文件开头的统一说明不能替代函数级说明" in document_xml
    assert "面向学习和面试讲解的实现指南" in document_xml
