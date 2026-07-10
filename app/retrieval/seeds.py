"""人工审核知识种子的标准 JSON 加载器。

标准 JSON 不支持注释，因此所有说明位于实现指南；加载时仍必须经过 KnowledgeSeedBundle
校验，确保类型白名单、source_span、唯一 ID 和边引用全部有效。
"""

import json
from pathlib import Path

from app.retrieval.models import KnowledgeSeedBundle


def load_knowledge_seed(path: Path) -> KnowledgeSeedBundle:
    if not path.is_file():
        raise FileNotFoundError(f"knowledge seed file does not exist: {path}")
    payload = json.loads(path.read_text(encoding="utf-8"))
    return KnowledgeSeedBundle.model_validate(payload)
