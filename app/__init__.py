"""DataOps Troubleshooter 应用根包。

这里仅保存跨模块都需要的应用版本，避免领域层依赖 FastAPI、MCP 或数据库实现。
版本会同时进入 API 元数据、健康检查和未来运行事件，便于演示与评测追溯。
"""

__version__ = "0.1.0"
