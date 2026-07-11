"""公开确定性报告草稿、规则审计和安全修订边界。

报告生成不调用 LLM，也不执行任何修复操作；它只把已验证状态投影为 DiagnosisReport。Auditor
负责语义审查，规则校验器负责引用和风险不变量，两者共同阻止无依据内容进入最终响应。
"""

from app.reporting.draft import DeterministicReportBuilder
from app.reporting.policy import ReportPolicyValidator
from app.reporting.revision import SafeReportReviser

__all__ = [
    "DeterministicReportBuilder",
    "ReportPolicyValidator",
    "SafeReportReviser",
]
