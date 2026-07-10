"""版本化 Prompt 资源加载器。

Prompt ID 与文本文件分离，便于 Golden Case 回归记录具体版本。运行时只从本包读取
受版本控制的模板，禁止在节点中临时拼接不可审计的大段提示词。
"""

from pathlib import Path

PLANNER_PROMPT_ID = "planner-react:v1"
PLANNER_PROMPT_PATH = Path(__file__).with_name("planner_react_v1.txt")


def load_planner_prompt() -> str:
    """读取受版本控制的 Planner Prompt 模板并原样返回。

    调用方负责在启动阶段检查非空内容和配置中的 Prompt ID；本函数只执行 UTF-8 资源读取，
    不在运行时拼接隐藏规则，从而让评测能够把一次决策准确关联到仓库中的固定文本版本。
    文件缺失或编码损坏会直接抛出标准 I/O 异常，避免静默退回未经审计的默认 Prompt。
    """

    return PLANNER_PROMPT_PATH.read_text(encoding="utf-8")


__all__ = ["PLANNER_PROMPT_ID", "load_planner_prompt"]
