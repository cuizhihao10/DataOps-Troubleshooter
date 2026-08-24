"""验证文档库加载器的仓库内语料完整性、路径逃逸防护与坏 manifest 显式失败。

语料缺一半的检索结果比明确报错危险得多：系统照常给出报告，只是永远不引用某份 Runbook。这些
测试因此既锁定"仓库自带 manifest 必须能真实加载且五份文档全部产出片段"，也锁定加载器拒绝
`../` 逃逸、缺文件、坏枚举与重复 doc_id。仓库语料用例同时充当种子数据的回归门禁：任何人改坏
Markdown 或 manifest 都会在这里失败，而不是等到 Docker 启动或评测阶段。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.retrieval.document_seeds import load_document_library
from app.retrieval.documents import MAX_CHUNK_CHARS, DocumentType

_MANIFEST = Path("data/knowledge/documents/manifest.json")


def test_repository_manifest_loads_every_document_with_valid_chunks() -> None:
    """验证仓库自带 manifest 与 Markdown 能加载成合法文档库，且四类文档来源齐备。

    Runbook/SOP 提供处置步骤、复盘提供已验证因果链、FAQ 提供判断依据，四类缺一都会让文档通道
    只覆盖一部分排障场景；逐份文档断言片段非空并满足长度上界，因为零片段文档在检索侧完全不可见。
    """

    library = load_document_library(_MANIFEST)

    assert library.library_version == "document-seed:v1"
    assert len(library.documents) == 5
    assert {document.doc_type for document in library.documents} == set(DocumentType)
    for document in library.documents:
        assert document.chunks, f"document {document.doc_id} produced no chunks"
        assert all(chunk.char_count <= MAX_CHUNK_CHARS for chunk in document.chunks)
        assert all(chunk.doc_id == document.doc_id for chunk in document.chunks)
        assert all(chunk.heading_path.startswith(document.title) for chunk in document.chunks)
        assert all(chunk.embedding is None for chunk in document.chunks)


def test_loaded_chunk_ids_are_globally_unique_across_the_library() -> None:
    """验证整库切片引用全局唯一，避免两份文档的 `dc_*` 脚注指向同一标识。

    引用 ID 由 `sha256(doc_id|ordinal)` 截断而成，理论上存在碰撞可能；一旦碰撞，报告脚注与数据库
    主键就会指向别的正文，而检索结果看不出任何异常，因此把唯一性作为语料级门禁固定下来。
    """

    library = load_document_library(_MANIFEST)

    chunk_ids = [chunk.chunk_id for document in library.documents for chunk in document.chunks]

    assert len(chunk_ids) == len(set(chunk_ids))


def _write_manifest(
    tmp_path: Path,
    entry: dict[str, object],
    *,
    markdown: str = "正文一段。",
) -> Path:
    """在临时目录写出一份单文档 manifest 与配套 Markdown，返回 manifest 路径。

    正文默认写入固定文件名，只有当被测 entry 指向别的 `path` 时才会缺文件，因此同一个辅助函数既能
    构造合法用例，也能构造缺文件与路径逃逸用例。
    """

    (tmp_path / "doc.md").write_text(markdown, encoding="utf-8")
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(
        json.dumps({"library_version": "document-seed:v1", "documents": [entry]}),
        encoding="utf-8",
    )
    return manifest_path


def _entry(**overrides: object) -> dict[str, object]:
    """构造一条合法 manifest 记录并允许逐字段覆盖，供各失败用例复用。

    默认取值全部合法，因此任何用例失败都能确定原因来自它覆盖的那一个字段，而不需要在多处对比
    完整 JSON。
    """

    entry: dict[str, object] = {
        "doc_id": "runbook_temp_case",
        "doc_type": "runbook",
        "title": "临时手册",
        "components": ["flashsync"],
        "source_id": "synthetic_temp_v1",
        "revision": "r1",
        "reliability": 0.9,
        "path": "doc.md",
    }
    entry.update(overrides)
    return entry


def test_relative_path_escaping_the_manifest_directory_is_rejected(tmp_path: Path) -> None:
    """验证 `../` 相对路径被拒绝，manifest 无法把仓库外任意文件当成知识语料读入。

    manifest 是数据文件而不是代码，如果它能引用任意路径，一份被篡改的 manifest 就等于一条任意文件
    读取通道，而读进来的内容会原样进入 Planner 上下文与报告引用。
    """

    outside = tmp_path.parent / "outside-secret.md"
    outside.write_text("不应被读取的仓库外内容。", encoding="utf-8")
    corpus = tmp_path / "corpus"
    corpus.mkdir()
    manifest_path = _write_manifest(corpus, _entry(path=f"../{outside.name}"))

    with pytest.raises(ValueError, match="escapes the manifest directory"):
        load_document_library(manifest_path)


def test_missing_files_and_malformed_manifests_fail_instead_of_skipping(tmp_path: Path) -> None:
    """验证缺失 manifest、缺失 Markdown、非对象负载与空 documents 数组都显式失败。

    加载器不跳过坏文档：跳过会让部署得到一个"看起来成功但少了一份 Runbook"的语料库，而这种缺失
    只会表现为召回率下降。四种形态各自对应一类真实事故（漏挂卷、漏提交文件、写错结构、清空数组）。
    """

    with pytest.raises(FileNotFoundError, match="document manifest does not exist"):
        load_document_library(tmp_path / "absent.json")

    with pytest.raises(FileNotFoundError, match="document file does not exist"):
        load_document_library(_write_manifest(tmp_path, _entry(path="missing.md")))

    list_payload = tmp_path / "list.json"
    list_payload.write_text("[]", encoding="utf-8")
    with pytest.raises(ValueError, match="must be a JSON object"):
        load_document_library(list_payload)

    empty_documents = tmp_path / "empty.json"
    empty_documents.write_text(
        json.dumps({"library_version": "document-seed:v1", "documents": []}),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="non-empty documents array"):
        load_document_library(empty_documents)


def test_unknown_document_type_and_missing_path_fail_at_load_time(tmp_path: Path) -> None:
    """验证未知 doc_type 与缺失 path 在加载阶段失败，而不是留到数据库约束才暴露。

    doc_type 与数据库 CheckConstraint 一一对应，拼错时留到写库才失败会让错误信息指向约束名而不是
    具体文档；缺 path 则会让加载器读到空路径，那种错误的可诊断性同样很差。
    """

    with pytest.raises(ValueError, match="is not a valid DocumentType"):
        load_document_library(_write_manifest(tmp_path, _entry(doc_type="playbook")))

    no_path = _entry()
    del no_path["path"]
    with pytest.raises(ValueError, match="requires a path"):
        load_document_library(_write_manifest(tmp_path, no_path))
