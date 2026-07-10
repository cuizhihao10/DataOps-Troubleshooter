"""人工审核知识种子的标准 JSON 加载器。

标准 JSON 不支持注释，因此所有说明位于实现指南；加载时仍必须经过 KnowledgeSeedBundle
校验，确保类型白名单、source_span、唯一 ID 和边引用全部有效。
"""

import json
from pathlib import Path

from app.retrieval.models import KnowledgeSeedBundle


def load_knowledge_seed(path: Path) -> KnowledgeSeedBundle:
    """从标准 UTF-8 JSON 读取人工知识种子并验证为完整图 Bundle。

    文件存在性先检查以给出明确路径错误，随后标准库解析 JSON，最后由 KnowledgeSeedBundle 验证
    类型白名单、唯一 ID、引用完整性和自环。任何失败都在连接数据库前传播，不跳过坏节点或边。
    """

    if not path.is_file():
        raise FileNotFoundError(f"knowledge seed file does not exist: {path}")

    # JSON 保持跨语言标准格式，解释性内容放在实现指南，结构正确性由 Pydantic 集中保证。
    payload = json.loads(path.read_text(encoding="utf-8"))
    return KnowledgeSeedBundle.model_validate(payload)
