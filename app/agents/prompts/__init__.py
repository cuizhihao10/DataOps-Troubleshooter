"""版本化 Prompt 资源加载器。

Prompt ID 与文本文件分离，便于 Golden Case 回归记录具体版本。运行时只从本包读取
受版本控制的模板，禁止在节点中临时拼接不可审计的大段提示词。
"""

from pathlib import Path

PLANNER_PROMPT_ID = "planner-react:v4"
PLANNER_SYSTEM_PROMPT_PATH = Path(__file__).with_name("planner_react_v4_system.txt")
PLANNER_USER_PROMPT_PATH = Path(__file__).with_name("planner_react_v4_user.txt")
AUDITOR_PROMPT_ID = "auditor-report:v2"
AUDITOR_SYSTEM_PROMPT_PATH = Path(__file__).with_name("auditor_report_v2_system.txt")
AUDITOR_USER_PROMPT_PATH = Path(__file__).with_name("auditor_report_v2_user.txt")


def load_planner_prompt_parts() -> tuple[str, str]:
    """读取 v4 Planner 的 system 与 user 两个受版本控制模板。

    v4 在会话上下文基础上加入确定性历史案例解释；system 模板只保存不可被运行数据覆盖的规则，
    user 模板保存全部占位符。缺失或编码错误直接抛 I/O 异常，不回退旧版本。
    """

    return (
        PLANNER_SYSTEM_PROMPT_PATH.read_text(encoding="utf-8"),
        PLANNER_USER_PROMPT_PATH.read_text(encoding="utf-8"),
    )


def load_planner_prompt() -> str:
    """读取受版本控制的 Planner Prompt 模板并原样返回。

    调用方负责在启动阶段检查非空内容和配置中的 Prompt ID；本函数只执行 UTF-8 资源读取，
    不在运行时拼接隐藏规则，从而让评测能够把一次决策准确关联到仓库中的固定文本版本。
    文件缺失或编码损坏会直接抛出标准 I/O 异常，避免静默退回未经审计的默认 Prompt。
    """

    system_prompt, user_prompt = load_planner_prompt_parts()
    return f"{system_prompt}\n\n{user_prompt}"


def load_auditor_prompt_parts() -> tuple[str, str]:
    """读取 v2 Auditor 的静态 system 与运行时 user 模板。

    v2 新增历史解释与实时事实优先审计；两个 UTF-8 文件分别固定角色规则和不可信审计数据。
    缺失、编码错误或空内容显式失败，不回退旧 Prompt 或拼接隐藏供应商指令。
    """

    return (
        AUDITOR_SYSTEM_PROMPT_PATH.read_text(encoding="utf-8"),
        AUDITOR_USER_PROMPT_PATH.read_text(encoding="utf-8"),
    )


def load_auditor_prompt() -> str:
    """组合读取 Auditor 两条模板，供启动完整性检查和文档门禁使用。

    运行时仍通过 `load_auditor_prompt_parts` 保持消息角色分离；本函数只提供非空审计视图，不执行
    format 或模型请求，因此不会把用户问题提升到 system 优先级。
    """

    system_prompt, user_prompt = load_auditor_prompt_parts()
    return f"{system_prompt}\n\n{user_prompt}"


__all__ = [
    "AUDITOR_PROMPT_ID",
    "PLANNER_PROMPT_ID",
    "load_auditor_prompt",
    "load_auditor_prompt_parts",
    "load_planner_prompt",
    "load_planner_prompt_parts",
]
