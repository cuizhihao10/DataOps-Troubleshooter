"""Planner 与 Auditor 两个 LLM Agent 的代码边界。

当前包只保留版本化 Prompt；真正节点会在 LangGraph 切片接入。输入校验、工具执行、
检索和记忆写入不会被包装成 Agent，以保持确定性执行和双 Agent 的产品边界。
"""
