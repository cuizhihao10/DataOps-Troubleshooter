from pathlib import Path

PLANNER_PROMPT_ID = "planner-react:v1"
PLANNER_PROMPT_PATH = Path(__file__).with_name("planner_react_v1.txt")


def load_planner_prompt() -> str:
    return PLANNER_PROMPT_PATH.read_text(encoding="utf-8")


__all__ = ["PLANNER_PROMPT_ID", "load_planner_prompt"]
