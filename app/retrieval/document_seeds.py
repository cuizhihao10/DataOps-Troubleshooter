"""文档库加载器：读取 manifest 与 Markdown 正文并确定性切片成文档库。

正文用 Markdown 单独存放而不是塞进 JSON 字符串，因为 Runbook/SOP 需要人工评审与 diff，转义后的
单行 JSON 无法阅读；结构化元数据（类型、组件、可靠性、修订号）则留在 manifest 里由 Pydantic 校验。
切片在加载阶段完成，因此"库里到底存了哪些片段"完全由代码决定且可重放，不依赖任何外部服务。
"""

from __future__ import annotations

import json
from pathlib import Path

from app.retrieval.chunking import chunk_markdown_document
from app.retrieval.documents import DocumentLibrary, DocumentType, KnowledgeDocument


def load_document_library(manifest_path: Path) -> DocumentLibrary:
    """按 manifest 读取全部 Markdown 文档，切片后组装成受校验的文档库。

    manifest 与正文分离，因此每份文档的 `path` 都相对 manifest 所在目录解析；缺文件、坏枚举或重复
    doc_id 一律在连接数据库前失败，不跳过坏文档——语料缺一半的检索结果比明确报错危险得多。
    """

    if not manifest_path.is_file():
        raise FileNotFoundError(f"document manifest does not exist: {manifest_path}")

    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("document manifest must be a JSON object")
    entries = payload.get("documents")
    if not isinstance(entries, list) or not entries:
        raise ValueError("document manifest must declare a non-empty documents array")

    base_dir = manifest_path.parent
    documents = [_load_document(base_dir, entry) for entry in entries]
    return DocumentLibrary(
        library_version=payload.get("library_version", ""),
        documents=documents,
    )


def _load_document(base_dir: Path, entry: object) -> KnowledgeDocument:
    """读取单条 manifest 记录对应的 Markdown 文件并切片成一份文档。

    这里只做"取值 + 交给切片器"，字段合法性全部由 `KnowledgeDocument` 的 Pydantic 校验负责，避免
    在加载器里重复一套弱化的类型检查；相对路径限制在 manifest 目录内，防止 manifest 引用仓库外文件。
    """

    if not isinstance(entry, dict):
        raise ValueError("document manifest entries must be JSON objects")

    relative_path = entry.get("path")
    if not isinstance(relative_path, str) or not relative_path.strip():
        raise ValueError("document manifest entry requires a path")
    # 解析后校验前缀，阻止 manifest 用 `../` 把仓库外的任意文件当成知识语料读进来。
    markdown_path = (base_dir / relative_path).resolve()
    if not markdown_path.is_relative_to(base_dir.resolve()):
        raise ValueError(f"document path escapes the manifest directory: {relative_path}")
    if not markdown_path.is_file():
        raise FileNotFoundError(f"document file does not exist: {markdown_path}")

    return chunk_markdown_document(
        doc_id=entry.get("doc_id", ""),
        doc_type=DocumentType(entry.get("doc_type", "")),
        title=entry.get("title", ""),
        components=entry.get("components", []),
        source_id=entry.get("source_id", ""),
        revision=entry.get("revision", ""),
        reliability=entry.get("reliability", 1),
        markdown=markdown_path.read_text(encoding="utf-8"),
    )
