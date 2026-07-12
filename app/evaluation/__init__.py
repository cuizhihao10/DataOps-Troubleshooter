"""公开版本化 Golden 诊断、作品集 manifest、执行器和汇总报告入口。

该包只评估公开诊断结果并编排既有评测测试，不创建新 Agent，也不改变生产运行时。调用方可加载
受控 manifest，或直接运行 Golden Case 评分，并生成明确区分 passed/failed/skipped 的报告。
"""

from app.evaluation.golden_diagnosis import (
    GOLDEN_DIAGNOSIS_CATEGORY_TARGETS,
    GOLDEN_DIAGNOSIS_EVAL_CONTRACT_ID,
    GOLDEN_DIAGNOSIS_TARGET_CASE_COUNT,
    GoldenDiagnosisCaseResult,
    GoldenDiagnosisEvalReport,
    GoldenDiagnosisRunner,
    evaluate_golden_diagnosis,
    score_golden_diagnosis_case,
)
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
    "GOLDEN_DIAGNOSIS_EVAL_CONTRACT_ID",
    "GOLDEN_DIAGNOSIS_CATEGORY_TARGETS",
    "GOLDEN_DIAGNOSIS_TARGET_CASE_COUNT",
    "PORTFOLIO_EVAL_MANIFEST_CONTRACT_ID",
    "PORTFOLIO_EVAL_RUN_CONTRACT_ID",
    "GoldenDiagnosisCaseResult",
    "GoldenDiagnosisEvalReport",
    "GoldenDiagnosisRunner",
    "PortfolioEvalManifest",
    "PortfolioEvalRunReport",
    "PortfolioMetricSnapshot",
    "PortfolioSuiteRun",
    "PortfolioSuiteSpec",
    "SubprocessPytestExecutor",
    "evaluate_golden_diagnosis",
    "load_portfolio_eval_manifest",
    "run_portfolio_evaluation",
    "score_golden_diagnosis_case",
]
