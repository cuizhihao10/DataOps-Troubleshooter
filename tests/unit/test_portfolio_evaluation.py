"""验证统一作品集评测 manifest、执行状态、指标发布门禁和数据库快速模式。

单元测试使用记录型 pytest 执行器，不创建子进程；它锁定五层覆盖、安全 target、delta、一层失败
隐藏指标、缺数据库 blocked，以及显式跳过 PostgreSQL 后报告不完整但已执行层仍可成功的语义。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from app.evaluation import (
    PortfolioEvalManifest,
    load_portfolio_eval_manifest,
    run_portfolio_evaluation,
)
from app.evaluation.portfolio import (
    PytestExecutionResult,
    SuiteExecutionStatus,
)

MANIFEST_PATH = Path("data/evals/portfolio_eval_manifest.json")


class RecordingPytestExecutor:
    """按预设退出码返回 pytest 结果，并记录无 shell 参数列表。

    输出队列耗尽会显式失败，避免统一运行器意外多执行 suite 却被默认成功掩盖；每次结果使用固定
    小耗时和合成摘要，不访问测试数据库或真实 pytest。
    """

    def __init__(self, return_codes: list[int]) -> None:
        """复制退出码脚本并初始化空命令记录。

        输入列表不会被就地消费；构造不执行任何命令。空列表合法但首次 execute 会失败，便于验证
        完全 skipped/blocked 场景没有启动子进程。
        """

        self._return_codes = list(return_codes)
        self.commands: list[list[str]] = []

    def execute(self, command: list[str]) -> PytestExecutionResult:
        """记录参数列表、消费一个退出码并返回强类型合成结果。

        非零结果携带失败摘要；零结果可携带普通通过摘要但上层不会把它当指标来源。脚本耗尽抛
        AssertionError，暴露额外执行或 skip/blocked 分支错误。
        """

        if not self._return_codes:
            raise AssertionError("portfolio runner executed more pytest suites than scripted")
        self.commands.append(list(command))
        return_code = self._return_codes.pop(0)
        return PytestExecutionResult(
            return_code=return_code,
            duration_ms=25,
            output_summary=("synthetic pytest passed" if return_code == 0 else "synthetic failure"),
        )


def test_portfolio_manifest_loads_five_layers_and_rejects_unsafe_test_target() -> None:
    """确认 v13 manifest 精确覆盖五层、十九个指标，并拒绝任意 pytest flag/命令目标。

    复制 payload 后把第一 target 改为 ``--collect-only``；Pydantic 必须在执行器之前失败，证明 JSON
    不能把受限 test target 字段变成自由命令入口。
    """

    manifest = load_portfolio_eval_manifest(MANIFEST_PATH)

    assert manifest.contract_id == "portfolio-eval-manifest:v13"
    assert len(manifest.suites) == 5
    assert sum(len(suite.metrics) for suite in manifest.suites) == 19
    assert sum(suite.requires_postgres for suite in manifest.suites) == 2

    payload = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    payload["suites"][0]["test_targets"] = ["--collect-only"]
    with pytest.raises(ValidationError, match="unsafe test targets"):
        PortfolioEvalManifest.model_validate(payload)


def test_portfolio_manifest_v1_remains_readable_with_exact_legacy_four_suites() -> None:
    """验证升级默认 v13 后仍可读取精确四层的历史 v1 manifest。

    测试从当前 JSON 删除 Golden 层并回写 v1 contract；兼容只允许旧精确集合，不能让任意缺层 v2
    借用 v1 标签通过。该能力用于解释旧结果，不会使默认 CLI 回退到四层。
    """

    payload = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    payload["contract_id"] = "portfolio-eval-manifest:v1"
    payload["suites"] = [
        suite for suite in payload["suites"] if suite["suite_id"] != "golden_diagnosis_baseline"
    ]

    manifest = PortfolioEvalManifest.model_validate(payload)

    assert manifest.contract_id == "portfolio-eval-manifest:v1"
    assert len(manifest.suites) == 4


def test_portfolio_manifest_v2_requires_the_original_golden_v1_source() -> None:
    """验证五层历史 v2 只能绑定不含路径指标的 Golden v1 来源契约。

    测试从当前 v13 JSON 删除后续指标并回写两个旧 contract；若只修改版本却保留 Golden v2
    来源，模型必须拒绝，防止旧消费者把新增字段误认为原 v2 语义。
    """

    payload = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    payload["contract_id"] = "portfolio-eval-manifest:v2"
    golden_suite = next(
        suite for suite in payload["suites"] if suite["suite_id"] == "golden_diagnosis_baseline"
    )
    golden_suite["source_contract_id"] = "golden-diagnosis-eval:v1"
    golden_suite["metrics"] = [
        metric
        for metric in golden_suite["metrics"]
        if metric["metric_id"]
        not in {
            "golden_fault_path_completeness",
            "golden_history_recall_coverage",
            "golden_realtime_priority_pass",
            "golden_evidence_conflict_safe_resolution",
        }
    ]
    coverage = next(
        metric
        for metric in golden_suite["metrics"]
        if metric["metric_id"] == "golden_case_coverage"
    )
    coverage["treatment_label"] = "measured_scripted_5_cases"
    coverage["treatment_value"] = 0.1786
    coverage["delta"] = -0.8214

    manifest = PortfolioEvalManifest.model_validate(payload)
    assert manifest.contract_id == "portfolio-eval-manifest:v2"

    golden_suite["source_contract_id"] = "golden-diagnosis-eval:v2"
    golden_suite["metrics"] = [
        metric
        for metric in golden_suite["metrics"]
        if metric["metric_id"]
        not in {
            "golden_history_recall_coverage",
            "golden_realtime_priority_pass",
            "golden_evidence_conflict_safe_resolution",
        }
    ]
    with pytest.raises(ValidationError, match="requires Golden source"):
        PortfolioEvalManifest.model_validate(payload)


def test_portfolio_manifest_v3_preserves_five_case_path_scoring_snapshot() -> None:
    """验证历史 v3 绑定 Golden v2 与 5/28 覆盖快照，不能借用当前 8 条数字。

    v3 已包含路径完整率但尚未增加类别配额和三条新案例；测试回写来源与覆盖值后应通过，再把
    coverage 改回当前 8/28 时必须失败，证明实测快照变化确实需要 v4。
    """

    payload = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    payload["contract_id"] = "portfolio-eval-manifest:v3"
    golden_suite = next(
        suite for suite in payload["suites"] if suite["suite_id"] == "golden_diagnosis_baseline"
    )
    golden_suite["source_contract_id"] = "golden-diagnosis-eval:v2"
    golden_suite["metrics"] = [
        metric
        for metric in golden_suite["metrics"]
        if metric["metric_id"]
        not in {
            "golden_history_recall_coverage",
            "golden_realtime_priority_pass",
            "golden_evidence_conflict_safe_resolution",
        }
    ]
    coverage = next(
        metric
        for metric in golden_suite["metrics"]
        if metric["metric_id"] == "golden_case_coverage"
    )
    coverage["treatment_label"] = "measured_scripted_5_cases"
    coverage["treatment_value"] = 0.1786
    coverage["delta"] = -0.8214

    manifest = PortfolioEvalManifest.model_validate(payload)
    assert manifest.contract_id == "portfolio-eval-manifest:v3"

    coverage["treatment_value"] = 0.2857
    coverage["delta"] = -0.7143
    with pytest.raises(ValidationError, match="requires Golden coverage snapshot"):
        PortfolioEvalManifest.model_validate(payload)


def test_portfolio_manifest_v4_preserves_eight_case_category_snapshot() -> None:
    """验证历史 v4 绑定 Golden v3、8/28 覆盖和不含记忆专用指标的集合。

    v4 已有类别配额但 memory 类别仍为零；测试回写旧来源、覆盖和七指标集合后应通过。保留当前
    两个记忆指标必须失败，防止旧报告被解释成已经完成长期记忆 Golden Cases。
    """

    payload = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    payload["contract_id"] = "portfolio-eval-manifest:v4"
    golden_suite = next(
        suite for suite in payload["suites"] if suite["suite_id"] == "golden_diagnosis_baseline"
    )
    golden_suite["source_contract_id"] = "golden-diagnosis-eval:v3"
    coverage = next(
        metric
        for metric in golden_suite["metrics"]
        if metric["metric_id"] == "golden_case_coverage"
    )
    coverage["treatment_label"] = "measured_scripted_8_cases"
    coverage["treatment_value"] = 0.2857
    coverage["delta"] = -0.7143

    with pytest.raises(ValidationError, match="versioned Golden metric set"):
        PortfolioEvalManifest.model_validate(payload)

    golden_suite["metrics"] = [
        metric
        for metric in golden_suite["metrics"]
        if metric["metric_id"]
        not in {
            "golden_history_recall_coverage",
            "golden_realtime_priority_pass",
            "golden_evidence_conflict_safe_resolution",
        }
    ]
    manifest = PortfolioEvalManifest.model_validate(payload)
    assert manifest.contract_id == "portfolio-eval-manifest:v4"


def test_portfolio_manifest_v5_preserves_eleven_case_memory_snapshot() -> None:
    """验证历史 v5 仍绑定 Golden v4、11/28 覆盖和不含证据冲突指标的九项集合。

    测试从当前 v13 payload 回写旧来源与覆盖值并删除新增指标，确认可读取；若保留 v6 冲突指标则
    必须失败，防止旧运行报告被重新解释为已经评测成功响应之间的事实冲突。
    """

    payload = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    payload["contract_id"] = "portfolio-eval-manifest:v5"
    golden_suite = next(
        suite for suite in payload["suites"] if suite["suite_id"] == "golden_diagnosis_baseline"
    )
    golden_suite["source_contract_id"] = "golden-diagnosis-eval:v4"
    coverage = next(
        metric
        for metric in golden_suite["metrics"]
        if metric["metric_id"] == "golden_case_coverage"
    )
    coverage["treatment_label"] = "measured_scripted_11_cases"
    coverage["treatment_value"] = 0.3929
    coverage["delta"] = -0.6071

    with pytest.raises(ValidationError, match="versioned Golden metric set"):
        PortfolioEvalManifest.model_validate(payload)

    golden_suite["metrics"] = [
        metric
        for metric in golden_suite["metrics"]
        if metric["metric_id"] != "golden_evidence_conflict_safe_resolution"
    ]
    manifest = PortfolioEvalManifest.model_validate(payload)
    assert manifest.contract_id == "portfolio-eval-manifest:v5"


def test_portfolio_manifest_v6_preserves_twelve_case_conflict_snapshot() -> None:
    """验证历史 v6 绑定 Golden v5、12/28 覆盖和首条成功响应冲突指标。

    v6 与当前 v13 的指标集合相同，但 Golden 来源和覆盖快照不同；测试先回写 12 条版本并确认可读，
    再改用 13/28 覆盖值，要求版本门禁失败，防止旧报告被解释成已包含第二条跨组件链路。
    """

    payload = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    payload["contract_id"] = "portfolio-eval-manifest:v6"
    golden_suite = next(
        suite for suite in payload["suites"] if suite["suite_id"] == "golden_diagnosis_baseline"
    )
    golden_suite["source_contract_id"] = "golden-diagnosis-eval:v5"
    coverage = next(
        metric
        for metric in golden_suite["metrics"]
        if metric["metric_id"] == "golden_case_coverage"
    )
    coverage["treatment_label"] = "measured_scripted_12_cases"
    coverage["treatment_value"] = 0.4286
    coverage["delta"] = -0.5714

    manifest = PortfolioEvalManifest.model_validate(payload)
    assert manifest.contract_id == "portfolio-eval-manifest:v6"

    coverage["treatment_value"] = 0.4643
    coverage["delta"] = -0.5357
    with pytest.raises(ValidationError, match="requires Golden coverage snapshot"):
        PortfolioEvalManifest.model_validate(payload)


def test_portfolio_manifest_v7_preserves_thirteen_case_lts_bds_snapshot() -> None:
    """验证历史 v7 绑定 Golden v6 与 13/28 的 LTS→BDS 跨组件快照。

    v7 与当前 v13 共享指标集合和 Golden Case Schema，但来源评测版本及覆盖率不同；回写 13 条快照
    后应可读取，若使用 14/28 当前覆盖则必须失败，防止旧报告被解释成已包含 BDS→FlashSync 案例。
    """

    payload = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    payload["contract_id"] = "portfolio-eval-manifest:v7"
    golden_suite = next(
        suite for suite in payload["suites"] if suite["suite_id"] == "golden_diagnosis_baseline"
    )
    golden_suite["source_contract_id"] = "golden-diagnosis-eval:v6"
    coverage = next(
        metric
        for metric in golden_suite["metrics"]
        if metric["metric_id"] == "golden_case_coverage"
    )
    coverage["treatment_label"] = "measured_scripted_13_cases"
    coverage["treatment_value"] = 0.4643
    coverage["delta"] = -0.5357

    manifest = PortfolioEvalManifest.model_validate(payload)
    assert manifest.contract_id == "portfolio-eval-manifest:v7"

    coverage["treatment_value"] = 0.5
    coverage["delta"] = -0.5
    with pytest.raises(ValidationError, match="requires Golden coverage snapshot"):
        PortfolioEvalManifest.model_validate(payload)


def test_portfolio_manifest_v8_preserves_fourteen_case_flashsync_snapshot() -> None:
    """验证历史 v8 绑定 Golden v7 与 14/28 的 BDS→FlashSync 快照。

    回写 v8 来源与覆盖率后 manifest 应保持可读；随后使用当前 15/28 覆盖必须失败，证明新增独立资源
    耗尽 Fixture 需要新的 Portfolio 版本，旧主键冲突快照不会被静默当作已包含该事实环境。
    """

    payload = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    payload["contract_id"] = "portfolio-eval-manifest:v8"
    golden_suite = next(
        suite for suite in payload["suites"] if suite["suite_id"] == "golden_diagnosis_baseline"
    )
    golden_suite["source_contract_id"] = "golden-diagnosis-eval:v7"
    coverage = next(
        metric
        for metric in golden_suite["metrics"]
        if metric["metric_id"] == "golden_case_coverage"
    )
    coverage["treatment_label"] = "measured_scripted_14_cases"
    coverage["treatment_value"] = 0.5
    coverage["delta"] = -0.5

    manifest = PortfolioEvalManifest.model_validate(payload)
    assert manifest.contract_id == "portfolio-eval-manifest:v8"

    coverage["treatment_value"] = 0.5357
    coverage["delta"] = -0.4643
    with pytest.raises(ValidationError, match="requires Golden coverage snapshot"):
        PortfolioEvalManifest.model_validate(payload)


def test_portfolio_manifest_v9_preserves_fifteen_case_resource_snapshot() -> None:
    """验证历史 v9 绑定 Golden v8 与 15/28 的独立资源耗尽快照。

    回写来源和覆盖后 v9 应可读取；改用 16/28 当前值必须失败，证明新增零工具补参案例不会被静默
    归入旧资源耗尽报告，即使两个版本的指标字段集合完全相同。
    """

    payload = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    payload["contract_id"] = "portfolio-eval-manifest:v9"
    golden_suite = next(
        suite for suite in payload["suites"] if suite["suite_id"] == "golden_diagnosis_baseline"
    )
    golden_suite["source_contract_id"] = "golden-diagnosis-eval:v8"
    coverage = next(
        metric
        for metric in golden_suite["metrics"]
        if metric["metric_id"] == "golden_case_coverage"
    )
    coverage["treatment_label"] = "measured_scripted_15_cases"
    coverage["treatment_value"] = 0.5357
    coverage["delta"] = -0.4643

    manifest = PortfolioEvalManifest.model_validate(payload)
    assert manifest.contract_id == "portfolio-eval-manifest:v9"

    coverage["treatment_value"] = 0.5714
    coverage["delta"] = -0.4286
    with pytest.raises(ValidationError, match="requires Golden coverage snapshot"):
        PortfolioEvalManifest.model_validate(payload)


def test_portfolio_manifest_v10_preserves_sixteen_case_clarification_snapshot() -> None:
    """验证历史 v10 绑定 Golden v9 与 16/28 的零工具补参快照。

    回写旧来源和覆盖率后应通过；再使用 v11 的 17/28 覆盖必须失败，防止“没有发起工具调用”和“发起
    三项工具但缺因果日志”两个不同安全降级边界共享同一版本快照。
    """

    payload = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    payload["contract_id"] = "portfolio-eval-manifest:v10"
    golden_suite = next(
        suite for suite in payload["suites"] if suite["suite_id"] == "golden_diagnosis_baseline"
    )
    golden_suite["source_contract_id"] = "golden-diagnosis-eval:v9"
    coverage = next(
        metric
        for metric in golden_suite["metrics"]
        if metric["metric_id"] == "golden_case_coverage"
    )
    coverage["treatment_label"] = "measured_scripted_16_cases"
    coverage["treatment_value"] = 0.5714
    coverage["delta"] = -0.4286

    manifest = PortfolioEvalManifest.model_validate(payload)
    assert manifest.contract_id == "portfolio-eval-manifest:v10"

    coverage["treatment_value"] = 0.6071
    coverage["delta"] = -0.3929
    with pytest.raises(ValidationError, match="requires Golden coverage snapshot"):
        PortfolioEvalManifest.model_validate(payload)


def test_portfolio_manifest_v11_preserves_seventeen_case_partial_evidence_snapshot() -> None:
    """验证历史 v11 绑定 Golden v10 与 17/28 的部分证据安全降级快照。

    回写 v11 的来源、覆盖率和标签后应通过；再替换为 v12 的 18/28 覆盖必须失败。该测试确保
    “部分成功 Observation”与“状态、日志、拓扑全部不可用”两个证据边界拥有独立可解释版本。
    """

    payload = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    # 从当前完整 manifest 只回写版本绑定字段，确保 suite/metric 集合与真实迁移路径一致。
    payload["contract_id"] = "portfolio-eval-manifest:v11"
    golden_suite = next(
        suite for suite in payload["suites"] if suite["suite_id"] == "golden_diagnosis_baseline"
    )
    golden_suite["source_contract_id"] = "golden-diagnosis-eval:v10"
    coverage = next(
        metric
        for metric in golden_suite["metrics"]
        if metric["metric_id"] == "golden_case_coverage"
    )
    coverage["treatment_label"] = "measured_scripted_17_cases"
    coverage["treatment_value"] = 0.6071
    coverage["delta"] = -0.3929

    manifest = PortfolioEvalManifest.model_validate(payload)
    assert manifest.contract_id == "portfolio-eval-manifest:v11"

    # 保持 v11 标签却注入 v12 覆盖值必须失败，证明版本号不能伪装更新后的评测数据。
    coverage["treatment_value"] = 0.6429
    coverage["delta"] = -0.3571
    with pytest.raises(ValidationError, match="requires Golden coverage snapshot"):
        PortfolioEvalManifest.model_validate(payload)


def test_portfolio_manifest_v12_preserves_eighteen_case_unavailable_snapshot() -> None:
    """验证历史 v12 绑定 Golden v11 与 18/28 的 LTS 全源不可用快照。

    v12 的覆盖率、来源与标签回写后应通过；注入 v13 的 19/28 覆盖必须失败。该边界避免新增明确
    LTS 参数根因和 graph-seed:v2 路径后，旧的纯安全降级评测被静默解释为已覆盖该能力。
    """

    payload = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    # 只回写版本绑定字段，保留真实 suite 和 metric 集合，模拟从当前文件读取历史快照。
    payload["contract_id"] = "portfolio-eval-manifest:v12"
    golden_suite = next(
        suite for suite in payload["suites"] if suite["suite_id"] == "golden_diagnosis_baseline"
    )
    golden_suite["source_contract_id"] = "golden-diagnosis-eval:v11"
    coverage = next(
        metric
        for metric in golden_suite["metrics"]
        if metric["metric_id"] == "golden_case_coverage"
    )
    coverage["treatment_label"] = "measured_scripted_18_cases"
    coverage["treatment_value"] = 0.6429
    coverage["delta"] = -0.3571

    manifest = PortfolioEvalManifest.model_validate(payload)
    assert manifest.contract_id == "portfolio-eval-manifest:v12"

    # v12 标签与 v13 数值的混搭必须由版本门禁拒绝，防止报告来源和案例集错位。
    coverage["treatment_value"] = 0.6786
    coverage["delta"] = -0.3214
    with pytest.raises(ValidationError, match="requires Golden coverage snapshot"):
        PortfolioEvalManifest.model_validate(payload)


def test_complete_portfolio_run_publishes_metrics_only_after_all_suites_pass() -> None:
    """验证完整模式五层通过后报告 complete、run_success 和 all_suites_passed 均为真。

    两个 PostgreSQL 命令必须追加内部 `-m postgres`，三个快速层不得追加；所有 passed suite 才携带
    manifest 指标，十九个指标全量进入本次报告。
    """

    manifest = load_portfolio_eval_manifest(MANIFEST_PATH)
    executor = RecordingPytestExecutor([0, 0, 0, 0, 0])

    report = run_portfolio_evaluation(
        manifest,
        executor,
        include_postgres=True,
        postgres_available=True,
    )

    assert report.run_success is True
    assert report.complete is True
    assert report.all_suites_passed is True
    assert all(suite.status is SuiteExecutionStatus.PASSED for suite in report.suites)
    assert sum(len(suite.metrics) for suite in report.suites) == 19
    assert sum("postgres" in command for command in executor.commands) == 2
    assert all(command[1:4] == ["-m", "pytest", "-q"] for command in executor.commands)


def test_fast_mode_skips_postgres_hides_metrics_and_remains_explicitly_incomplete() -> None:
    """验证 ``--skip-postgres`` 等价模式只执行三层，跳过层不展示旧实测数字。

    已执行 history/auditor/golden 层通过，因此 run_success=True；但两个 skipped 使 complete 与
    all_suites_passed=False。该组合允许快速反馈，却不能作为完整作品集成绩。
    """

    manifest = load_portfolio_eval_manifest(MANIFEST_PATH)
    executor = RecordingPytestExecutor([0, 0, 0])

    report = run_portfolio_evaluation(
        manifest,
        executor,
        include_postgres=False,
        postgres_available=False,
    )

    assert report.run_success is True
    assert report.complete is False
    assert report.all_suites_passed is False
    assert len(executor.commands) == 3
    skipped = [suite for suite in report.suites if suite.status is SuiteExecutionStatus.SKIPPED]
    assert len(skipped) == 2
    assert all(not suite.metrics for suite in skipped)
    assert all("skipped" in (suite.failure_summary or "").lower() for suite in skipped)


def test_complete_mode_without_database_marks_postgres_suites_blocked() -> None:
    """验证默认完整模式缺少测试数据库时不静默切换快速模式。

    两个 PostgreSQL suite 标记 blocked 且隐藏指标，非数据库层仍执行以提供诊断信息；最终
    run_success/complete/all_suites_passed 全为 false，提示用户补齐前置后重跑。
    """

    manifest = load_portfolio_eval_manifest(MANIFEST_PATH)
    executor = RecordingPytestExecutor([0, 0, 0])

    report = run_portfolio_evaluation(
        manifest,
        executor,
        include_postgres=True,
        postgres_available=False,
    )

    assert report.run_success is False
    assert report.complete is False
    assert report.all_suites_passed is False
    blocked = [suite for suite in report.suites if suite.status is SuiteExecutionStatus.BLOCKED]
    assert len(blocked) == 2
    assert all(not suite.metrics for suite in blocked)
    assert all("DATAOPS_TEST_DATABASE_URL" in (suite.failure_summary or "") for suite in blocked)
    assert len(executor.commands) == 3


def test_failed_suite_hides_snapshot_metrics_and_makes_complete_run_unsuccessful() -> None:
    """确认 pytest 非零退出后该层不携带 manifest 数字，即使其他层全部通过。

    所有 suite 均已执行，因此 complete=True；一个 failed 使 run_success 和 all_suites_passed 都为
    False。失败摘要保留合成输出，便于定位但不能与历史指标同时出现。
    """

    manifest = load_portfolio_eval_manifest(MANIFEST_PATH)
    executor = RecordingPytestExecutor([0, 1, 0, 0, 0])

    report = run_portfolio_evaluation(
        manifest,
        executor,
        include_postgres=True,
        postgres_available=True,
    )

    assert report.run_success is False
    assert report.complete is True
    assert report.all_suites_passed is False
    failed = [suite for suite in report.suites if suite.status is SuiteExecutionStatus.FAILED]
    assert len(failed) == 1
    assert failed[0].metrics == []
    assert failed[0].failure_summary == "synthetic failure"
