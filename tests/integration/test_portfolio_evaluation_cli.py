"""通过真实 Python 子进程验证统一作品集评测 CLI 的快速模式。

测试执行 ``python -m app.evaluation --skip-postgres``；CLI 再以无 shell 子进程运行 History、Auditor
与 Golden 三个评测节点。GraphRAG/记忆层必须明确 skipped，证明统一 JSON 不把旧数据库数字
冒充本次通过结果，也不递归执行当前 CLI 测试。
"""

from __future__ import annotations

import json
import subprocess
import sys

from app.evaluation.portfolio import SuiteExecutionStatus


def test_portfolio_cli_fast_mode_runs_three_suites_and_marks_report_incomplete() -> None:
    """验证快速 CLI 返回零退出码、三层 passed、两层 skipped 和不完整标记。

    子进程 stdout 必须是可解析的 `portfolio-eval-run:v2` JSON；passed 层发布当前已验证指标，
    skipped PostgreSQL 层指标为空。stderr 保留为空，防止运行时 warning 污染一键演示体验。
    """

    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "app.evaluation",
            "--skip-postgres",
            "--timeout-seconds",
            "60",
        ],
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=150,
        shell=False,
    )

    assert completed.returncode == 0, completed.stderr or completed.stdout
    assert completed.stderr == ""
    report = json.loads(completed.stdout)
    assert report["contract_id"] == "portfolio-eval-run:v2"
    assert report["manifest_contract_id"] == "portfolio-eval-manifest:v2"
    assert report["metric_kind"] == "measured"
    assert report["run_success"] is True
    assert report["complete"] is False
    assert report["all_suites_passed"] is False
    statuses = [suite["status"] for suite in report["suites"]]
    assert statuses.count(SuiteExecutionStatus.PASSED.value) == 3
    assert statuses.count(SuiteExecutionStatus.SKIPPED.value) == 2
    assert sum(len(suite["metrics"]) for suite in report["suites"]) == 11
    assert all(
        suite["metrics"] == []
        for suite in report["suites"]
        if suite["status"] == SuiteExecutionStatus.SKIPPED.value
    )
