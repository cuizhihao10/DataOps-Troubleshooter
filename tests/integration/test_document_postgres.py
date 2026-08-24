"""PostgreSQL 文档 RAG 集成测试：幂等导入、切片替换、双通道召回与向量空间隔离。

该测试使用 postgres marker 与快速测试隔离，真实检查文档表与切片表的迁移能力、`documents` /
`document_chunks` 的外键与唯一约束、全文 `ts_rank` 召回、pgvector cosine 召回，以及重新切片后
"先删该文档全部切片再整批插入"是否真的不留旧尾部。这些行为在单元测试里全部无法覆盖：它们依赖
真实 SQL、真实索引表达式和真实约束，而它们出错的表现都是"检索照常返回结果，只是内容过时或缺失"。
"""

import os
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from app.persistence.database import create_database_engine, create_session_factory
from app.retrieval.document_repository import PostgresDocumentRepository
from app.retrieval.document_seeds import load_document_library
from app.retrieval.document_service import DocumentRetrievalService
from app.retrieval.documents import DocumentType
from app.retrieval.embeddings import DeterministicHashEmbeddingProvider, embed_document_library
from app.retrieval.scoring import RetrievalChannel

DATABASE_URL = os.getenv("DATAOPS_TEST_DATABASE_URL")
MANIFEST = Path("data/knowledge/documents/manifest.json")


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_postgres_document_ingestion_search_and_chunk_replacement() -> None:
    """验证真实 PostgreSQL 中文档导入、约束、双通道召回与切片整批替换的闭环。

    测试先整库导入并提交，确认切片数与"当前 Provider 空间已嵌入切片数"完全相等——两者不等意味着
    语义通道只会少召回而不会报错。随后直接篡改维度确认 CheckConstraint 仍生效，用全文与向量两路
    查询证明排序发生在数据库而不是 Python，最后在未提交事务中把一份文档重新切成更少的片段，验证
    旧尾部切片不会以过时正文继续参与召回。rollback 与 finally 保证隔离和连接释放。
    """

    if DATABASE_URL is None:
        pytest.fail("DATAOPS_TEST_DATABASE_URL is required for postgres tests")

    engine = create_database_engine(DATABASE_URL)
    factory = create_session_factory(engine)
    try:
        async with factory() as session:
            repository = PostgresDocumentRepository(session)
            embedding_provider = DeterministicHashEmbeddingProvider(dimensions=128)

            # Markdown 与 manifest 保持无向量以便人工评审，入库前才按当前 Provider 整库嵌入。
            library = load_document_library(MANIFEST)
            embedded_library = await embed_document_library(library, embedding_provider)
            await repository.upsert_document_library(embedded_library)
            await session.commit()

            document_count, chunk_count = await repository.count_documents()
            expected_chunks = sum(len(document.chunks) for document in library.documents)
            assert document_count == len(library.documents)
            assert chunk_count == expected_chunks
            assert (
                await repository.count_embedded_chunks(
                    provider_id=embedding_provider.provider_id,
                    dimensions=embedding_provider.dimensions,
                )
                == expected_chunks
            )

            # 绕过 Pydantic 直接改维度，确认数据库仍拒绝"维度与向量长度不一致"的切片元数据。
            with pytest.raises(IntegrityError):
                await session.execute(
                    text(
                        "UPDATE document_chunks SET embedding_dimensions = 127 "
                        "WHERE doc_id = 'runbook_flashsync_primary_key_conflict'"
                    )
                )
            await session.rollback()

            # 未知 doc_type 必须被 CheckConstraint 拒绝，否则枚举与数据库会各自演化。
            with pytest.raises(IntegrityError):
                await session.execute(
                    text(
                        "UPDATE documents SET doc_type = 'playbook' "
                        "WHERE doc_id = 'runbook_flashsync_primary_key_conflict'"
                    )
                )
            await session.rollback()

            # 全文通道：中文短语必须命中主键冲突手册，且分数由 SQL 计算后回填。
            lexical_matches = await repository.search_lexical_chunks("主键冲突", limit=8)
            assert lexical_matches
            assert any(
                match.document.doc_id == "runbook_flashsync_primary_key_conflict"
                for match in lexical_matches
            )
            assert all(match.lexical_score > 0 for match in lexical_matches)
            assert all(match.chunk.embedding is None for match in lexical_matches)

            # 向量通道：排序由 pgvector cosine distance 完成，返回结果不携带原始向量。
            query_embedding = (await embedding_provider.embed_texts(["主键冲突处置步骤"]))[0]
            vector_matches = await repository.search_vector_chunks(
                query_embedding,
                provider_id=embedding_provider.provider_id,
                limit=8,
            )
            assert vector_matches
            assert all(match.embedding_dimensions == 128 for match in vector_matches)
            assert all(
                match.embedding_provider == embedding_provider.provider_id
                for match in vector_matches
            )
            assert all(match.chunk.embedding is None for match in vector_matches)
            assert all(0 <= match.semantic_score <= 1 for match in vector_matches)

            # 不同 Provider ID 必须返回空结果：两个向量空间即使维度相同也不能放进同一次排序。
            assert (
                await repository.search_vector_chunks(
                    query_embedding,
                    provider_id="different-space:v1",
                    limit=8,
                )
                == []
            )

            # 端到端服务：默认无重排，因此 reranker_model 必须真实为空且 final 等于 hybrid。
            service = DocumentRetrievalService(repository, embedding_provider)
            result = await service.retrieve("FlashSync 主键冲突处置步骤", chunk_limit=4)
            assert result.contract_id == "document-retrieval:v1"
            assert result.reranker_model is None
            assert result.rerank_blend_weight == 0
            assert 1 <= len(result.chunks) <= 4
            assert result.candidate_count >= len(result.chunks)
            assert all(chunk.final_score == chunk.hybrid_score for chunk in result.chunks)
            assert all(chunk.rerank_score is None for chunk in result.chunks)
            assert all(chunk.authority_score > 0 for chunk in result.chunks)
            assert any(
                RetrievalChannel.VECTOR in chunk.channels for chunk in result.chunks
            )
            top = result.chunks[0]
            assert top.document.doc_type in set(DocumentType)
            assert top.chunk.chunk_id.startswith("dc_")
            assert top.chunk.char_count == len(top.chunk.content)

            # 消融：在未提交事务里把该文档重新切成单个片段，旧尾部切片必须整批消失而不是残留。
            target = next(
                document
                for document in library.documents
                if document.doc_id == "runbook_flashsync_primary_key_conflict"
            )
            assert len(target.chunks) > 1
            shrunk = target.model_copy(update={"chunks": [target.chunks[0]]})
            shrunk_library = embedded_library.model_copy(update={"documents": [shrunk]})
            reembedded = await embed_document_library(shrunk_library, embedding_provider)
            await repository.upsert_document_library(reembedded)
            await session.flush()

            remaining = await session.scalar(
                text(
                    "SELECT count(*) FROM document_chunks "
                    "WHERE doc_id = 'runbook_flashsync_primary_key_conflict'"
                )
            )
            assert remaining == 1
            after_document_count, after_chunk_count = await repository.count_documents()
            assert after_document_count == document_count
            assert after_chunk_count == chunk_count - len(target.chunks) + 1
            await session.rollback()

            # 回滚后语料必须回到导入后的完整状态，证明消融真的只发生在事务内。
            assert await repository.count_documents() == (document_count, chunk_count)
    finally:
        # 即使断言失败也关闭 asyncpg 池，防止后续测试因连接泄漏出现假故障。
        await engine.dispose()
