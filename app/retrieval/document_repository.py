"""PostgreSQL 文档与文档切片仓储：幂等导入、全文召回与向量召回。

文档域与知识图共用同一个数据库和同一个 embedding Provider 契约，但保持独立表与独立仓储：切片
是"一段可执行步骤"，节点是"一个实体"，把两者混在一张表里会让向量空间过滤和评分因子互相污染。

导入采取"先删该文档全部切片再整批插入"的语义而不是逐条 upsert，因为重新切片会改变切片数量，
残留的旧尾部切片会以过时正文继续参与召回；删除依赖外键级联之外的显式 DELETE，使同一事务内
即可观察到干净状态。检索侧的全文与向量查询与 GraphRAG 使用完全相同的分数换算与绑定参数纪律。
"""

from __future__ import annotations

from math import isfinite

from sqlalchemy import delete, func, select, text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.persistence.models import DocumentChunkRecord, DocumentRecord
from app.retrieval.documents import (
    DocumentChunk,
    DocumentLibrary,
    DocumentMetadata,
    LexicalChunkMatch,
    VectorChunkMatch,
)


class PostgresDocumentRepository:
    """封装文档库 upsert、切片计数、全文切片召回和向量切片召回。

    仓储只做数据库查询与 Record/领域模型转换，不生成处置结论；AsyncSession 由调用方管理事务，
    写入不自动 commit，因此整库导入可以原子提交，集成测试也能在事务内删数据后回滚做消融对比。
    """

    def __init__(self, session: AsyncSession) -> None:
        """注入调用方拥有的异步会话，使文档导入与知识图导入可共享同一事务。

        构造器不打开连接、不提交也不回滚。与图仓储保持相同的所有权边界，`seed_database` 因此能把
        "知识图 + 文档库"作为一次原子导入，避免部署后出现只有一半语料的检索状态。
        """

        self._session = session

    async def upsert_document_library(self, library: DocumentLibrary) -> None:
        """按文档主键 upsert 元数据，并整批替换每份文档的切片，但不提交事务。

        切片先删后插而不是逐条 upsert：重新切片会改变片段数量与边界，逐条 upsert 会让旧版本的尾部
        切片以过时正文残留在库里继续被召回，而这种污染在检索结果上完全看不出来。
        """

        for document in library.documents:
            values = {
                "doc_id": document.doc_id,
                "doc_type": document.doc_type.value,
                "title": document.title,
                "components": document.components,
                "source_id": document.source_id,
                "revision": document.revision,
                "reliability": document.reliability,
            }
            statement = insert(DocumentRecord).values(**values)
            statement = statement.on_conflict_do_update(
                index_elements=[DocumentRecord.doc_id],
                set_={**values, "updated_at": func.now()},
            )
            await self._session.execute(statement)

            # 同一事务内先清空旧切片，使 (doc_id, ordinal) 唯一约束不会与上一版切片边界冲突。
            await self._session.execute(
                delete(DocumentChunkRecord).where(DocumentChunkRecord.doc_id == document.doc_id)
            )
            for chunk in document.chunks:
                await self._session.execute(
                    insert(DocumentChunkRecord).values(
                        chunk_id=chunk.chunk_id,
                        doc_id=chunk.doc_id,
                        ordinal=chunk.ordinal,
                        heading_path=chunk.heading_path,
                        content=chunk.content,
                        char_count=chunk.char_count,
                        embedding=chunk.embedding,
                        embedding_provider=chunk.embedding_provider,
                        embedding_dimensions=chunk.embedding_dimensions,
                    )
                )

    async def count_documents(self) -> tuple[int, int]:
        """查询当前事务视图中的文档数与切片数，供健康检查报告语料规模。

        两个独立 COUNT 让健康检查能区分"文档没导入"和"文档导入了但切片为空"两种故障；`or 0` 防守
        方言或测试替身返回 None。方法不提交事务，也不加载任何正文或向量内容。
        """

        document_count = await self._session.scalar(
            select(func.count()).select_from(DocumentRecord)
        )
        chunk_count = await self._session.scalar(
            select(func.count()).select_from(DocumentChunkRecord)
        )
        return int(document_count or 0), int(chunk_count or 0)

    async def count_embedded_chunks(self, *, provider_id: str, dimensions: int) -> int:
        """统计当前 Provider/维度空间中已具有非空向量的切片数量。

        过滤条件与 cosine 查询完全一致，因此该数字不会把旧 Provider 或其他维度的历史记录误报为当前
        空间可用数据；健康检查据此确认种子命令真的完成了向量回填，而不是只看到切片总数。
        """

        if not provider_id.strip():
            raise ValueError("provider_id must not be blank")
        if not 8 <= dimensions <= 4096:
            raise ValueError("dimensions must be between 8 and 4096")

        count = await self._session.scalar(
            select(func.count())
            .select_from(DocumentChunkRecord)
            .where(
                DocumentChunkRecord.embedding.is_not(None),
                DocumentChunkRecord.embedding_provider == provider_id,
                DocumentChunkRecord.embedding_dimensions == dimensions,
            )
        )
        return int(count or 0)

    async def search_lexical_chunks(
        self,
        query: str,
        *,
        limit: int = 8,
    ) -> list[LexicalChunkMatch]:
        """用 PostgreSQL 全文排名与标题/正文包含 bonus 召回有界文档切片。

        标题路径与正文进入同一个 tsvector，与迁移里的 GIN 表达式索引完全一致，否则索引不会被使用；
        LIKE bonus 补足 `websearch_to_tsquery('simple')` 对中文和短组件名切分能力不足的部分。
        """

        if not query.strip():
            raise ValueError("query must not be blank")
        if not 1 <= limit <= 40:
            raise ValueError("limit must be between 1 and 40")

        # 表达式必须与迁移中的 GIN 索引逐字符一致，否则 PostgreSQL 会退化为顺序扫描。
        statement = text(
            """
            WITH ranked AS (
                SELECT
                    c.chunk_id,
                    c.doc_id,
                    c.ordinal,
                    c.heading_path,
                    c.content,
                    c.char_count,
                    d.doc_type,
                    d.title,
                    d.components,
                    d.source_id,
                    d.revision,
                    d.reliability,
                    ts_rank(
                        to_tsvector(
                            'simple',
                            coalesce(c.heading_path, '') || ' ' || coalesce(c.content, '')
                        ),
                        websearch_to_tsquery('simple', :query)
                    ) AS text_rank,
                    CASE
                        WHEN lower(c.heading_path) LIKE lower(:pattern) THEN 0.5
                        WHEN lower(c.content) LIKE lower(:pattern) THEN 0.25
                        ELSE 0
                    END AS lexical_bonus
                FROM document_chunks c
                JOIN documents d ON d.doc_id = c.doc_id
            )
            SELECT *, greatest(text_rank + lexical_bonus, 0.001) AS lexical_score
            FROM ranked
            WHERE text_rank > 0 OR lexical_bonus > 0
            ORDER BY lexical_score DESC, reliability DESC, chunk_id
            LIMIT :limit
            """
        )
        # 查询文本、LIKE 模式和 limit 全部走绑定参数，用户输入不参与 SQL 结构拼接。
        result = await self._session.execute(
            statement,
            {"query": query, "pattern": f"%{query}%", "limit": limit},
        )
        return [
            LexicalChunkMatch(
                document=_document_from_mapping(row._mapping),
                chunk=_chunk_from_mapping(row._mapping),
                lexical_score=float(row.lexical_score),
            )
            for row in result
        ]

    async def search_vector_chunks(
        self,
        query_embedding: list[float],
        *,
        provider_id: str,
        limit: int = 8,
    ) -> list[VectorChunkMatch]:
        """用 pgvector cosine distance 在兼容 Provider/维度空间内召回语义相近的切片。

        SQL 先按 Provider ID 与实际向量长度过滤，避免不同模型之间做无意义比较；距离由数据库计算并
        转成 `[0, 1]` 相似度，负相关裁剪到零以保持与图侧完全相同的评分契约，不在 Python 里扫全表。
        """

        if not query_embedding:
            raise ValueError("query_embedding must not be empty")
        if not all(isinstance(value, int | float) and isfinite(value) for value in query_embedding):
            raise ValueError("query_embedding values must be finite numbers")
        if not any(value != 0 for value in query_embedding):
            raise ValueError("query_embedding must not be an all-zero vector")
        if not 1 <= limit <= 40:
            raise ValueError("limit must be between 1 and 40")
        if not provider_id.strip():
            raise ValueError("provider_id must not be blank")

        distance = DocumentChunkRecord.embedding.cosine_distance(query_embedding)
        statement = (
            select(DocumentChunkRecord, DocumentRecord, distance.label("cosine_distance"))
            .join(DocumentRecord, DocumentRecord.doc_id == DocumentChunkRecord.doc_id)
            .where(
                DocumentChunkRecord.embedding.is_not(None),
                DocumentChunkRecord.embedding_provider == provider_id,
                DocumentChunkRecord.embedding_dimensions == len(query_embedding),
            )
            .order_by(
                distance,
                DocumentRecord.reliability.desc(),
                DocumentChunkRecord.chunk_id,
            )
            .limit(limit)
        )
        result = await self._session.execute(statement)

        matches: list[VectorChunkMatch] = []
        for chunk_record, document_record, raw_distance in result:
            # cosine similarity 理论范围是 [-1, 1]，检索分数把负相关裁剪到零以保持评分契约闭合。
            semantic_score = max(0.0, min(1.0, 1.0 - float(raw_distance)))
            matches.append(
                VectorChunkMatch(
                    document=_document_from_record(document_record),
                    chunk=_chunk_from_record(chunk_record),
                    embedding_provider=chunk_record.embedding_provider,
                    embedding_dimensions=chunk_record.embedding_dimensions,
                    semantic_score=semantic_score,
                )
            )
        return matches


def _document_from_mapping(mapping) -> DocumentMetadata:
    """把原生 SQL RowMapping 里的文档列转换成受校验的文档元数据模型。

    检索路径每次只关心命中的那一个切片，因此这里不回查同文档其它切片，也不构造任何占位正文——
    检索结果中出现凭空拼装的片段会直接污染证据链，元数据与切片分离正是为了排除这种可能。
    """

    return DocumentMetadata(
        doc_id=mapping["doc_id"],
        doc_type=mapping["doc_type"],
        title=mapping["title"],
        components=mapping["components"],
        source_id=mapping["source_id"],
        revision=mapping["revision"],
        reliability=mapping["reliability"],
    )


def _document_from_record(record: DocumentRecord) -> DocumentMetadata:
    """把 SQLAlchemy 文档 Record 转换成与协议层无关的文档元数据模型。

    显式字段映射避免 ORM 内部状态泄漏进检索结果；向量查询已在 SQL 里 JOIN 出文档行，因此这里不会
    为每个命中切片再触发一次元数据查询，也就不存在 N+1 问题。
    """

    return DocumentMetadata(
        doc_id=record.doc_id,
        doc_type=record.doc_type,
        title=record.title,
        components=record.components,
        source_id=record.source_id,
        revision=record.revision,
        reliability=record.reliability,
    )


def _chunk_from_mapping(mapping) -> DocumentChunk:
    """把原生 SQL RowMapping 转换为不携带原始向量的受校验切片模型。

    全文查询不选择 embedding 列，避免驱动把未声明类型的 vector 解码成文本，也避免上下文重复携带
    大数组；缺列或长度不一致仍由 Pydantic 在仓储边界显式拒绝，而不是进入评分阶段才出错。
    """

    return DocumentChunk(
        chunk_id=mapping["chunk_id"],
        doc_id=mapping["doc_id"],
        ordinal=mapping["ordinal"],
        heading_path=mapping["heading_path"],
        content=mapping["content"],
        char_count=mapping["char_count"],
    )


def _chunk_from_record(record: DocumentChunkRecord) -> DocumentChunk:
    """把 SQLAlchemy 切片 Record 转换成领域切片模型，并刻意丢弃 embedding 数组。

    向量只用于数据库距离计算，Provider 与维度由 `VectorChunkMatch` 单独保留，因此检索结果既不会
    泄漏派生模型特征，也不会让上下文预算被几千个浮点数挤占。
    """

    return DocumentChunk(
        chunk_id=record.chunk_id,
        doc_id=record.doc_id,
        ordinal=record.ordinal,
        heading_path=record.heading_path,
        content=record.content,
        char_count=record.char_count,
    )
