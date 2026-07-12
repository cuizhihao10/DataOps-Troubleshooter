"""公开版本化作品集评测 manifest、执行器和汇总报告入口。

该包只编排已经存在的 GraphRAG、长期记忆、历史影响和 Auditor 消融测试，不创建新 Agent，也不
改变生产运行时。调用方可加载受控 manifest 并生成一份明确区分 passed/failed/skipped 的报告。
"""

from app.evaluation.portfolio import (
    PORTFOLIO_EVAL_MANIFEST_CONTRACT_ID,
    PORTFOLIO_EVAL_RUN_CONTRACT_ID,
    PortfolioEvalManifest,
    PortfolioEvalRunReport,
    PortfolioMetricSnapshot,
    PortfolioSuiteRun,
    PortfolioSuiteSpec,
    SubprocessPytestExecutor,
    load_portfolio_eval_manifest,
    run_portfolio_evaluation,
)

__all__ = [
    "PORTFOLIO_EVAL_MANIFEST_CONTRACT_ID",
    "PORTFOLIO_EVAL_RUN_CONTRACT_ID",
    "PortfolioEvalManifest",
    "PortfolioEvalRunReport",
    "PortfolioMetricSnapshot",
    "PortfolioSuiteRun",
    "PortfolioSuiteSpec",
    "SubprocessPytestExecutor",
    "load_portfolio_eval_manifest",
    "run_portfolio_evaluation",
]
