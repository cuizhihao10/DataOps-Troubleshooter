"""版本化 Prompt 资源加载器。

Prompt ID 与文本文件分离，便于 Golden Case 回归记录具体版本。运行时只从本包读取
受版本控制的模板，禁止在节点中临时拼接不可审计的大段提示词。
"""

from pathlib import Path

PLANNER_PROMPT_ID = "planner-react:v1"
PLANNER_PROMPT_PATH = Path(__file__).with_name("planner_react_v1.txt")


def load_planner_prompt() -> str:
    return PLANNER_PROMPT_PATH.read_text(encoding="utf-8")


__all__ = ["PLANNER_PROMPT_ID", "load_planner_prompt"]
