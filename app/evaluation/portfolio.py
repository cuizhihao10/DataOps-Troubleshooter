"""用单条命令执行并汇总五层版本化作品集评测。

manifest 固定每层测试入口、数据库前置、结果文档和已审核实测快照；执行器以无 shell 的参数列表
运行 pytest。只有本次测试通过的层才携带指标，失败、跳过或前置阻塞不会展示旧数字冒充本次成绩。
CLI 默认要求 PostgreSQL 完整运行，也提供明确不完整的 ``--skip-postgres`` 快速模式。
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from collections.abc import Sequence
from enum import StrEnum
from pathlib import Path
from time import perf_counter
from typing import Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, model_validator

PORTFOLIO_EVAL_MANIFEST_CONTRACT_ID = "portfolio-eval-manifest:v5"
PORTFOLIO_EVAL_RUN_CONTRACT_ID = "portfolio-eval-run:v5"
DEFAULT_MANIFEST_PATH = Path("data/evals/portfolio_eval_manifest.json")
_V1_REQUIRED_SUITE_IDS = {
    "graphrag_ablation",
    "memory_recall_ablation",
    "history_impact_ablation",
    "auditor_impact_ablation",
}
_V2_REQUIRED_SUITE_IDS = _V1_REQUIRED_SUITE_IDS | {"golden_diagnosis_baseline"}
_REQUIRED_SUITE_IDS_BY_CONTRACT = {
    "portfolio-eval-manifest:v1": _V1_REQUIRED_SUITE_IDS,
    "portfolio-eval-manifest:v2": _V2_REQUIRED_SUITE_IDS,
    "portfolio-eval-manifest:v3": _V2_REQUIRED_SUITE_IDS,
    "portfolio-eval-manifest:v4": _V2_REQUIRED_SUITE_IDS,
    "portfolio-eval-manifest:v5": _V2_REQUIRED_SUITE_IDS,
}
_GOLDEN_SOURCE_CONTRACT_BY_MANIFEST = {
    "portfolio-eval-manifest:v2": "golden-diagnosis-eval:v1",
    "portfolio-eval-manifest:v3": "golden-diagnosis-eval:v2",
    "portfolio-eval-manifest:v4": "golden-diagnosis-eval:v3",
    "portfolio-eval-manifest:v5": "golden-diagnosis-eval:v4",
}
_GOLDEN_COVERAGE_VALUE_BY_MANIFEST = {
    "portfolio-eval-manifest:v2": 0.1786,
    "portfolio-eval-manifest:v3": 0.1786,
    "portfolio-eval-manifest:v4": 0.2857,
    "portfolio-eval-manifest:v5": 0.3929,
}
_GOLDEN_V2_METRIC_IDS = {
    "golden_case_coverage",
    "golden_intent_accuracy",
    "golden_root_cause_top1",
    "golden_necessary_action_coverage",
    "golden_citation_completeness",
    "golden_safe_degradation",
}
_GOLDEN_REQUIRED_METRIC_IDS_BY_MANIFEST = {
    "portfolio-eval-manifest:v2": _GOLDEN_V2_METRIC_IDS,
    "portfolio-eval-manifest:v3": _GOLDEN_V2_METRIC_IDS | {"golden_fault_path_completeness"},
    "portfolio-eval-manifest:v4": _GOLDEN_V2_METRIC_IDS | {"golden_fault_path_completeness"},
    "portfolio-eval-manifest:v5": _GOLDEN_V2_METRIC_IDS
    | {
        "golden_fault_path_completeness",
        "golden_history_recall_coverage",
        "golden_realtime_priority_pass",
    },
}
_TEST_TARGET = re.compile(r"^tests/[a-zA-Z0-9_./-]+\.py(?:::[a-zA-Z0-9_\[\]-]+)?$")


class MetricDirection(StrEnum):
    """说明一个实测指标通常应提高、降低或只作为上下文展示。

    方向不参与自动通过判定，因为当前数字是实测快照而非目标阈值；它帮助作品集读者正确解释
    正负 delta，例如危险残留率下降是改善，而 Recall 上升才是改善。
    """

    HIGHER_IS_BETTER = "higher_is_better"
    LOWER_IS_BETTER = "lower_is_better"
    CONTEXT_ONLY = "context_only"


class PortfolioMetricSnapshot(BaseModel):
    """保存一个已审核指标的两组实测值、差值、方向和适用边界。

    snapshot 只有对应 suite 本次 pytest 通过时才进入运行报告。delta 必须等于 treatment-control，
    防止 manifest、实测文档和 CLI 汇总采用不同符号约定。
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    metric_id: str = Field(pattern=r"^[a-z][a-z0-9_]{2,79}$")
    label: str = Field(min_length=1, max_length=200)
    control_label: str = Field(min_length=1, max_length=100)
    treatment_label: str = Field(min_length=1, max_length=100)
    control_value: float
    treatment_value: float
    delta: float
    direction: MetricDirection
    note: str = Field(min_length=1, max_length=1000)

    @model_validator(mode="after")
    def validate_delta(self) -> PortfolioMetricSnapshot:
        """校验差值采用统一 treatment-control 公式并容忍 JSON 浮点舍入。

        误差上限只吸收 4 位小数快照的舍入，不允许任意手写差值；失败发生在运行测试前，避免最终
        报告同时展示互相矛盾的控制组、实验组和 delta。
        """

        expected = self.treatment_value - self.control_value
        if abs(self.delta - expected) > 1e-4:
            raise ValueError("portfolio metric delta must equal treatment minus control")
        return self


class PortfolioSuiteSpec(BaseModel):
    """描述一个评测层的来源契约、pytest 入口、数据库要求和指标快照。

    test target 只能引用仓库 tests 下的 Python 文件或单个测试节点；它不是自由命令。结果文档与
    source contract 让汇总数字可以回到详细实验条件，不把不同层强行合并成一个总准确率。
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    suite_id: str = Field(pattern=r"^[a-z][a-z0-9_]{2,79}$")
    layer: str = Field(min_length=1, max_length=200)
    source_contract_id: str = Field(pattern=r"^[a-z0-9-]+:v\d+$")
    result_document: str = Field(pattern=r"^docs/[a-z0-9_-]+\.md$")
    requires_postgres: bool
    test_targets: list[str] = Field(min_length=1)
    metrics: list[PortfolioMetricSnapshot] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_targets_and_metric_ids(self) -> PortfolioSuiteSpec:
        """拒绝任意 pytest 参数、重复 target 和重复 metric ID。

        target 必须匹配受限仓库路径，不允许以 ``-`` 开头或插入 shell 字符；执行器随后仍使用
        ``shell=False``。重复项会让同一测试/指标被重复计数，因此在加载 manifest 时直接失败。
        """

        if len(self.test_targets) != len(set(self.test_targets)):
            raise ValueError("portfolio suite test targets must be unique")
        invalid_targets = [
            target for target in self.test_targets if not _TEST_TARGET.fullmatch(target)
        ]
        if invalid_targets:
            raise ValueError(f"portfolio suite contains unsafe test targets: {invalid_targets}")
        metric_ids = [metric.metric_id for metric in self.metrics]
        if len(metric_ids) != len(set(metric_ids)):
            raise ValueError("portfolio suite metric IDs must be unique")
        return self


class PortfolioEvalManifest(BaseModel):
    """封装版本化作品集评测层并保证 suite/metric 全局身份唯一。

    v1 保留原四层；v2 增加 Golden；v3 增加路径；v4 扩到 8 条；v5 补齐 3 条记忆类别案例。
    版本与精确 suite、Golden 来源和覆盖快照绑定，旧 JSON 不会被静默解释成当前完整运行。
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    contract_id: Literal[
        "portfolio-eval-manifest:v1",
        "portfolio-eval-manifest:v2",
        "portfolio-eval-manifest:v3",
        "portfolio-eval-manifest:v4",
        "portfolio-eval-manifest:v5",
    ]
    suites: list[PortfolioSuiteSpec] = Field(min_length=4)

    @model_validator(mode="after")
    def validate_suite_coverage(self) -> PortfolioEvalManifest:
        """按 manifest 版本检查精确 suite 集合与全局 metric ID 唯一性。

        精确集合防止 v1/v2 语义混用；全局 metric 唯一让 JSON 消费方无需同时使用 suite 作为复合
        键。任一冲突都会阻止执行，避免漏层报告仍被标为 complete。
        """

        suite_ids = [suite.suite_id for suite in self.suites]
        if len(suite_ids) != len(set(suite_ids)):
            raise ValueError("portfolio eval suite IDs must be unique")
        required_suite_ids = _REQUIRED_SUITE_IDS_BY_CONTRACT[self.contract_id]
        if set(suite_ids) != required_suite_ids:
            raise ValueError(
                f"{self.contract_id} must contain exactly its approved evaluation suites"
            )
        expected_golden_source = _GOLDEN_SOURCE_CONTRACT_BY_MANIFEST.get(self.contract_id)
        if expected_golden_source is not None:
            golden_suite = next(
                suite for suite in self.suites if suite.suite_id == "golden_diagnosis_baseline"
            )
            if golden_suite.source_contract_id != expected_golden_source:
                raise ValueError(
                    f"{self.contract_id} requires Golden source {expected_golden_source}"
                )
            coverage_metric = next(
                (
                    metric
                    for metric in golden_suite.metrics
                    if metric.metric_id == "golden_case_coverage"
                ),
                None,
            )
            expected_coverage = _GOLDEN_COVERAGE_VALUE_BY_MANIFEST[self.contract_id]
            if (
                coverage_metric is None
                or abs(coverage_metric.treatment_value - expected_coverage) > 1e-4
            ):
                raise ValueError(
                    f"{self.contract_id} requires Golden coverage snapshot {expected_coverage}"
                )
            expected_metric_ids = _GOLDEN_REQUIRED_METRIC_IDS_BY_MANIFEST[self.contract_id]
            actual_metric_ids = {metric.metric_id for metric in golden_suite.metrics}
            if actual_metric_ids != expected_metric_ids:
                raise ValueError(f"{self.contract_id} requires its versioned Golden metric set")
        metric_ids = [metric.metric_id for suite in self.suites for metric in suite.metrics]
        if len(metric_ids) != len(set(metric_ids)):
            raise ValueError("portfolio eval metric IDs must be globally unique")
        return self


class SuiteExecutionStatus(StrEnum):
    """区分单层测试通过、失败、主动跳过和缺少前置四种结果。

    skipped 只来自显式快速模式；blocked 表示用户请求完整运行但缺数据库。两者都不携带旧指标，
    failed 表示 pytest 已执行但未通过，passed 才允许发布 manifest 快照。
    """

    PASSED = "passed"
    FAILED = "failed"
    SKIPPED = "skipped"
    BLOCKED = "blocked"


class PytestExecutionResult(BaseModel):
    """保存无 shell pytest 子进程的退出码、耗时和截断公开输出。

    该对象由真实执行器或单元测试替身返回；output 只用于失败定位，不进入 passed suite 指标来源。
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    return_code: int
    duration_ms: int = Field(ge=0)
    output_summary: str = Field(default="", max_length=2000)


class PortfolioSuiteRun(BaseModel):
    """保存一次 suite 的状态、执行时间和仅在通过时公开的指标。

    Pydantic 交叉校验阻止 failed/skipped/blocked 携带 snapshot，也阻止 passed 带失败摘要；因此消费者
    无需猜测数字是本次验证结果还是旧文档缓存。
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    suite_id: str
    source_contract_id: str
    status: SuiteExecutionStatus
    requires_postgres: bool
    duration_ms: int = Field(ge=0)
    test_targets: list[str] = Field(min_length=1)
    metrics: list[PortfolioMetricSnapshot] = Field(default_factory=list)
    failure_summary: str | None = Field(default=None, min_length=1, max_length=2000)

    @model_validator(mode="after")
    def validate_status_payload(self) -> PortfolioSuiteRun:
        """绑定执行状态与 metrics/failure summary 的唯一合法组合。

        passed 必须有指标且无错误；其他状态必须隐藏指标并解释原因。该规则是防止失败后仍宣传旧
        百分比的最后一道结构化门禁。
        """

        if self.status is SuiteExecutionStatus.PASSED:
            if not self.metrics or self.failure_summary is not None:
                raise ValueError("passed portfolio suite requires metrics and no failure summary")
            return self
        if self.metrics or self.failure_summary is None:
            raise ValueError("non-passed portfolio suite must hide metrics and explain the status")
        return self


class PortfolioEvalRunReport(BaseModel):
    """汇总一次统一执行的版本化 suite 状态、指标发布资格和完整性。

    ``run_success`` 表示没有 failed/blocked，允许快速模式以零退出码反馈；``complete`` 表示无
    skipped/blocked；``all_suites_passed`` 只有 manifest 声明的全部层通过才为真。
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    contract_id: Literal["portfolio-eval-run:v5"]
    manifest_contract_id: Literal[
        "portfolio-eval-manifest:v1",
        "portfolio-eval-manifest:v2",
        "portfolio-eval-manifest:v3",
        "portfolio-eval-manifest:v4",
        "portfolio-eval-manifest:v5",
    ]
    metric_kind: Literal["measured"] = "measured"
    suites: list[PortfolioSuiteRun] = Field(min_length=4)
    run_success: bool
    complete: bool
    all_suites_passed: bool

    @model_validator(mode="after")
    def validate_summary_flags(self) -> PortfolioEvalRunReport:
        """从 suite 状态重算三个汇总布尔值，拒绝调用方手工美化结果。

        failed 或 blocked 使 run_success=False；skipped/blocked 使 complete=False；只有全部
        passed 才能 all_suites_passed=True。任何不一致都会使 CLI 构造失败而不是输出矛盾报告。
        """

        statuses = [suite.status for suite in self.suites]
        expected_success = not any(
            status in {SuiteExecutionStatus.FAILED, SuiteExecutionStatus.BLOCKED}
            for status in statuses
        )
        expected_complete = not any(
            status in {SuiteExecutionStatus.SKIPPED, SuiteExecutionStatus.BLOCKED}
            for status in statuses
        )
        expected_all_passed = all(status is SuiteExecutionStatus.PASSED for status in statuses)
        if self.run_success != expected_success:
            raise ValueError("portfolio run_success does not match suite statuses")
        if self.complete != expected_complete:
            raise ValueError("portfolio complete does not match suite statuses")
        if self.all_suites_passed != expected_all_passed:
            raise ValueError("portfolio all_suites_passed does not match suite statuses")
        return self


class PytestExecutor(Protocol):
    """声明统一运行器需要的最小同步 pytest 执行接口。

    生产实现调用 subprocess 参数列表，测试替身返回固定结果；协议不接收自由 shell 字符串，避免
    manifest 绕过 test target 校验。
    """

    def execute(self, command: list[str]) -> PytestExecutionResult:
        """执行一个已由 manifest 构造的 pytest 参数列表并返回公开结果。

        实现必须捕获退出码和耗时，不能抛弃失败或把非零退出转换为成功；超时可映射为约定非零码。
        """

        ...


class SubprocessPytestExecutor:
    """使用 ``subprocess.run(shell=False)`` 执行受限 pytest 命令。

    执行器继承当前环境以读取测试数据库 URL，但命令和报告均不打印环境值。超时返回 124 和公开
    摘要，使其他 suite 仍可运行并在最终报告中标记失败。
    """

    def __init__(self, *, timeout_seconds: int = 300) -> None:
        """保存单层最大执行时间，拒绝非正或超过一小时的预算。

        构造不启动进程；集中预算避免每个 suite 自行设置不同魔法数字。无效值立即失败。
        成功时只保存整数秒预算，不返回对象之外的资源，也不读取数据库配置。
        """

        if timeout_seconds <= 0 or timeout_seconds > 3600:
            raise ValueError("portfolio pytest timeout must be between 1 and 3600 seconds")
        self._timeout_seconds = timeout_seconds

    def execute(self, command: list[str]) -> PytestExecutionResult:
        """无 shell 执行 pytest，截断输出并把超时转换为稳定非零结果。

        command 已由内部函数构造；stdout/stderr 合并捕获，避免测试进度污染 JSON stdout。执行器不
        记录环境变量或命令之外的进程信息，编程级 OSError 继续传播供 CLI 显式失败。
        """

        started = perf_counter()
        try:
            completed = subprocess.run(
                command,
                check=False,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=self._timeout_seconds,
                shell=False,
            )
        except subprocess.TimeoutExpired:
            duration_ms = int((perf_counter() - started) * 1000)
            return PytestExecutionResult(
                return_code=124,
                duration_ms=duration_ms,
                output_summary=f"pytest exceeded {self._timeout_seconds} seconds",
            )
        duration_ms = int((perf_counter() - started) * 1000)
        combined = "\n".join(part for part in (completed.stdout, completed.stderr) if part)
        return PytestExecutionResult(
            return_code=completed.returncode,
            duration_ms=duration_ms,
            output_summary=_tail_output(combined),
        )


def load_portfolio_eval_manifest(path: Path) -> PortfolioEvalManifest:
    """加载 manifest，并验证引用的仓库测试文件和结果文档真实存在。

    JSON/Pydantic/唯一性错误原样传播；随后把 ``::`` 节点目标拆回文件路径，阻止拼写错误延迟到
    子进程。该函数不读取结果文档中的数字，指标快照一致性由文档门禁和各层测试共同锁定。
    """

    if not path.is_file():
        raise FileNotFoundError(f"portfolio eval manifest does not exist: {path}")
    payload = json.loads(path.read_text(encoding="utf-8"))
    manifest = PortfolioEvalManifest.model_validate(payload)
    for suite in manifest.suites:
        result_document = Path(suite.result_document)
        if not result_document.is_file():
            raise FileNotFoundError(f"portfolio result document does not exist: {result_document}")
        for target in suite.test_targets:
            target_file = Path(target.split("::", maxsplit=1)[0])
            if not target_file.is_file():
                raise FileNotFoundError(f"portfolio pytest target does not exist: {target_file}")
    return manifest


def run_portfolio_evaluation(
    manifest: PortfolioEvalManifest,
    executor: PytestExecutor,
    *,
    include_postgres: bool,
    postgres_available: bool,
) -> PortfolioEvalRunReport:
    """顺序执行 manifest 声明的测试层，并仅为本次 passed suite 发布指标。

    PostgreSQL 层在显式快速模式中 skipped；完整模式缺 URL 时 blocked。其余 suite 继续运行以提供
    最大诊断信息。报告状态完全由执行结果推导，不因某层失败而复用旧数字。
    """

    suite_runs: list[PortfolioSuiteRun] = []
    for suite in manifest.suites:
        if suite.requires_postgres and not include_postgres:
            suite_runs.append(
                _non_passed_suite(
                    suite,
                    status=SuiteExecutionStatus.SKIPPED,
                    summary="PostgreSQL suite skipped by explicit fast mode.",
                )
            )
            continue
        if suite.requires_postgres and not postgres_available:
            suite_runs.append(
                _non_passed_suite(
                    suite,
                    status=SuiteExecutionStatus.BLOCKED,
                    summary="DATAOPS_TEST_DATABASE_URL is required for a complete portfolio run.",
                )
            )
            continue

        # 命令只由当前解释器、固定 pytest 模块、受限 target 和内部 marker 组成。
        # manifest 不能提供 flags，避免版本化数据扩大为任意 pytest 参数入口。
        command = [sys.executable, "-m", "pytest", "-q", *suite.test_targets]
        if suite.requires_postgres:
            command.extend(["-m", "postgres"])
        execution = executor.execute(command)
        if execution.return_code == 0:
            suite_runs.append(
                PortfolioSuiteRun(
                    suite_id=suite.suite_id,
                    source_contract_id=suite.source_contract_id,
                    status=SuiteExecutionStatus.PASSED,
                    requires_postgres=suite.requires_postgres,
                    duration_ms=execution.duration_ms,
                    test_targets=suite.test_targets,
                    metrics=suite.metrics,
                )
            )
        else:
            summary = execution.output_summary or f"pytest exited with code {execution.return_code}"
            suite_runs.append(
                _non_passed_suite(
                    suite,
                    status=SuiteExecutionStatus.FAILED,
                    summary=summary,
                    duration_ms=execution.duration_ms,
                )
            )

    statuses = [suite.status for suite in suite_runs]
    run_success = not any(
        status in {SuiteExecutionStatus.FAILED, SuiteExecutionStatus.BLOCKED} for status in statuses
    )
    complete = not any(
        status in {SuiteExecutionStatus.SKIPPED, SuiteExecutionStatus.BLOCKED}
        for status in statuses
    )
    return PortfolioEvalRunReport(
        contract_id=PORTFOLIO_EVAL_RUN_CONTRACT_ID,
        manifest_contract_id=manifest.contract_id,
        suites=suite_runs,
        run_success=run_success,
        complete=complete,
        all_suites_passed=all(status is SuiteExecutionStatus.PASSED for status in statuses),
    )


def _non_passed_suite(
    suite: PortfolioSuiteSpec,
    *,
    status: SuiteExecutionStatus,
    summary: str,
    duration_ms: int = 0,
) -> PortfolioSuiteRun:
    """构造 failed/skipped/blocked suite，并强制隐藏 snapshot 指标。

    helper 不接受 passed 状态，避免调用方绕过正常执行路径创建无指标成功；summary 说明失败或未
    执行原因，最终模型再次验证组合。
    """

    if status is SuiteExecutionStatus.PASSED:
        raise ValueError("non-passed portfolio helper cannot construct passed suites")
    return PortfolioSuiteRun(
        suite_id=suite.suite_id,
        source_contract_id=suite.source_contract_id,
        status=status,
        requires_postgres=suite.requires_postgres,
        duration_ms=duration_ms,
        test_targets=suite.test_targets,
        metrics=[],
        failure_summary=summary,
    )


def _tail_output(value: str, *, max_chars: int = 2000) -> str:
    """保留 pytest 合并输出的末尾并去除首尾空白，限制公开失败摘要大小。

    末尾通常包含失败断言与汇总，比开头收集进度更有用；空输出合法返回空串，由上层补充退出码。
    函数不读取或替换环境变量，因为执行器从未把环境写入输出。
    """

    normalized = value.strip()
    return normalized[-max_chars:]


def _build_parser() -> argparse.ArgumentParser:
    """创建统一评测 CLI 参数解析器并解释完整/快速模式差异。

    parser 只开放 manifest 路径、跳过 PostgreSQL 和单层超时；不允许从命令行注入任意 pytest 参数。
    """

    parser = argparse.ArgumentParser(description="Run versioned DataOps portfolio evaluations.")
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST_PATH)
    parser.add_argument(
        "--skip-postgres",
        action="store_true",
        help="Run only non-PostgreSQL suites and mark the report incomplete.",
    )
    parser.add_argument("--timeout-seconds", type=int, default=300)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """解析 CLI、执行统一评测并把结构化 JSON 报告写到 stdout。

    完整运行缺数据库或任一测试失败返回 1；显式快速模式只要已执行 suite 通过就返回 0，但报告的
    complete/all_suites_passed 仍为 false。异常配置不被吞掉，由 Python 显示堆栈供开发者修复。
    """

    args = _build_parser().parse_args(argv)
    manifest = load_portfolio_eval_manifest(args.manifest)
    report = run_portfolio_evaluation(
        manifest,
        SubprocessPytestExecutor(timeout_seconds=args.timeout_seconds),
        include_postgres=not args.skip_postgres,
        postgres_available=bool(os.getenv("DATAOPS_TEST_DATABASE_URL")),
    )
    # Windows 交互终端常用 GBK，而 pytest 摘要和合成数据允许完整 Unicode；强制 UTF-8 让 stdout
    # 始终是可移植 JSON。StringIO 等测试替身没有 reconfigure，此时沿用调用方提供的文本边界。
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    print(report.model_dump_json(indent=2))
    return 0 if report.run_success else 1


if __name__ == "__main__":
    raise SystemExit(main())
