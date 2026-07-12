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
    """确认 v3 manifest 精确覆盖五层、十六个指标，并拒绝任意 pytest flag/命令目标。

    复制 payload 后把第一 target 改为 ``--collect-only``；Pydantic 必须在执行器之前失败，证明 JSON
    不能把受限 test target 字段变成自由命令入口。
    """

    manifest = load_portfolio_eval_manifest(MANIFEST_PATH)

    assert manifest.contract_id == "portfolio-eval-manifest:v3"
    assert len(manifest.suites) == 5
    assert sum(len(suite.metrics) for suite in manifest.suites) == 16
    assert sum(suite.requires_postgres for suite in manifest.suites) == 2

    payload = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    payload["suites"][0]["test_targets"] = ["--collect-only"]
    with pytest.raises(ValidationError, match="unsafe test targets"):
        PortfolioEvalManifest.model_validate(payload)


def test_portfolio_manifest_v1_remains_readable_with_exact_legacy_four_suites() -> None:
    """验证升级默认 v2 后仍可读取精确四层的历史 v1 manifest。

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

    测试从当前 v3 JSON 删除链路指标并回写两个旧 contract；若只修改 manifest 版本却保留 Golden v2
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
        if metric["metric_id"] != "golden_fault_path_completeness"
    ]

    manifest = PortfolioEvalManifest.model_validate(payload)
    assert manifest.contract_id == "portfolio-eval-manifest:v2"

    golden_suite["source_contract_id"] = "golden-diagnosis-eval:v2"
    with pytest.raises(ValidationError, match="requires Golden source"):
        PortfolioEvalManifest.model_validate(payload)


def test_complete_portfolio_run_publishes_metrics_only_after_all_suites_pass() -> None:
    """验证完整模式五层通过后报告 complete、run_success 和 all_suites_passed 均为真。

    两个 PostgreSQL 命令必须追加内部 `-m postgres`，两个快速层不得追加；所有 passed suite 才携带
    manifest 指标，九个指标全量进入本次报告。
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
    assert sum(len(suite.metrics) for suite in report.suites) == 16
    assert sum("postgres" in command for command in executor.commands) == 2
    assert all(command[1:4] == ["-m", "pytest", "-q"] for command in executor.commands)


def test_fast_mode_skips_postgres_hides_metrics_and_remains_explicitly_incomplete() -> None:
    """验证 ``--skip-postgres`` 等价模式只执行两层，跳过层不展示旧实测数字。

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
