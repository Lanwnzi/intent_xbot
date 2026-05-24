"""文档 RAG 索引器：SQLite 元数据 + Chroma 向量 + FTS5 全文检索。

独立于 memory_module_v2，使用独立的 Chroma collection "documents" 和 SQLite database。
"""

from __future__ import annotations

import json
import logging
import os
import re
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from langchain_core.documents import Document as LCDocument
from langchain_core.embeddings import Embeddings

from config import get_settings
from graph.llm import build_embedding_config_from_settings, get_embedding_model

logger = logging.getLogger(__name__)

# ── 路径常量 ────────────────────────────────────────────

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
DB_PATH = DATA_DIR / "documents.db"
CHROMA_PERSIST_DIR = DATA_DIR / "storage" / "chroma_documents"
DOCUMENTS_COLLECTION = "documents"

# ── 切分配置 ────────────────────────────────────────────

MAX_CHUNK_CHARS = 1200       # 约 300-400 中文 tokens
CHUNK_OVERLAP_CHARS = 120     # 滑窗 overlap
CLAUSE_PATTERN = re.compile(
    r"(?:^|\n)(第[零一二三四五六七八九十百千\d]+[条章款节]|第\s*\d+\s*[条章款节]|\d+\.\s*\S)"
)

# ── SQLite DDL ──────────────────────────────────────────

DDL = """
CREATE TABLE IF NOT EXISTS documents (
    doc_id       TEXT PRIMARY KEY,
    doc_name     TEXT NOT NULL,
    source_path  TEXT NOT NULL,
    content_type TEXT NOT NULL DEFAULT 'md',
    status       TEXT NOT NULL DEFAULT 'ingesting',
    session_id   TEXT,
    batch_id     TEXT,
    project_id   TEXT,
    company_id   TEXT,
    chunk_count  INTEGER DEFAULT 0,
    char_count   INTEGER DEFAULT 0,
    created_at   TEXT NOT NULL,
    error_message TEXT
);

CREATE INDEX IF NOT EXISTS idx_docs_status ON documents(status);
CREATE INDEX IF NOT EXISTS idx_docs_session ON documents(session_id);

CREATE TABLE IF NOT EXISTS document_chunks (
    rowid         INTEGER PRIMARY KEY AUTOINCREMENT,
    chunk_id      TEXT NOT NULL UNIQUE,
    doc_id        TEXT NOT NULL REFERENCES documents(doc_id),
    chunk_index   INTEGER NOT NULL,
    doc_name      TEXT NOT NULL,
    content       TEXT NOT NULL,
    section_title TEXT,
    token_count   INTEGER DEFAULT 0,
    source_path   TEXT,
    session_id    TEXT,
    batch_id      TEXT,
    project_id    TEXT,
    company_id    TEXT
);

CREATE INDEX IF NOT EXISTS idx_chunks_doc ON document_chunks(doc_id);
CREATE INDEX IF NOT EXISTS idx_chunks_project ON document_chunks(project_id);

-- FTS5 全文索引（doc_id / project_id 为前置过滤列，UNINDEXED 表示不参与全文分词）
-- 先删旧表再重建（兼容迁移）
DROP TABLE IF EXISTS document_chunks_fts;
CREATE VIRTUAL TABLE IF NOT EXISTS document_chunks_fts USING fts5(
    content,
    section_title,
    doc_name,
    doc_id UNINDEXED,
    project_id UNINDEXED,
    content='document_chunks',
    content_rowid='rowid'
);

-- 触发器：INSERT 自动同步 FTS（含过滤字段）
CREATE TRIGGER IF NOT EXISTS chunks_ai AFTER INSERT ON document_chunks BEGIN
    INSERT INTO document_chunks_fts(rowid, content, section_title, doc_name, doc_id, project_id)
    VALUES (new.rowid, new.content, new.section_title, new.doc_name, new.doc_id, new.project_id);
END;

-- 触发器：DELETE 自动同步 FTS
CREATE TRIGGER IF NOT EXISTS chunks_ad AFTER DELETE ON document_chunks BEGIN
    INSERT INTO document_chunks_fts(document_chunks_fts, rowid, content, section_title, doc_name, doc_id, project_id)
    VALUES ('delete', old.rowid, old.content, old.section_title, old.doc_name, old.doc_id, old.project_id);
END;
"""


class DocumentIndexer:
    """文档 RAG 索引器：入库、切分、向量化、检索。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._vector_store: Any = None
        self._embedding: Embeddings | None = None
        self._init_db()

    # ── 数据库初始化 ────────────────────────────────────

    def _init_db(self) -> None:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        CHROMA_PERSIST_DIR.mkdir(parents=True, exist_ok=True)
        with self._lock:
            conn = sqlite3.connect(str(DB_PATH))
            conn.executescript(DDL)
            # FTS5 表被 DROP+CREATE 重建后，已有 chunks 需重新同步
            conn.execute(
                "INSERT INTO document_chunks_fts(rowid, content, section_title, doc_name, doc_id, project_id) "
                "SELECT rowid, content, section_title, doc_name, doc_id, project_id FROM document_chunks"
            )
            conn.commit()
            conn.close()

    def _get_conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(DB_PATH))
        conn.row_factory = sqlite3.Row
        return conn

    # ── Embedding ────────────────────────────────────────

    def _get_embedding(self) -> Embeddings:
        if self._embedding is None:
            settings = get_settings()
            config = build_embedding_config_from_settings(settings)
            self._embedding = get_embedding_model(config)
        return self._embedding

    def _get_vector_store(self) -> Any:
        if self._vector_store is None:
            try:
                from langchain_chroma import Chroma

                self._vector_store = Chroma(
                    collection_name=DOCUMENTS_COLLECTION,
                    embedding_function=self._get_embedding(),
                    persist_directory=str(CHROMA_PERSIST_DIR),
                )
            except Exception as exc:
                logger.error("Failed to init Chroma: %s", exc)
                self._vector_store = None
        return self._vector_store

    # ── ID 生成 ──────────────────────────────────────────

    def _generate_doc_id(self) -> str:
        today = datetime.now(timezone.utc).strftime("%Y%m%d")
        conn = self._get_conn()
        row = conn.execute(
            "SELECT COUNT(*) AS cnt FROM documents WHERE doc_id LIKE ?",
            (f"DOC-{today}-%",),
        ).fetchone()
        conn.close()
        seq = (row["cnt"] if row else 0) + 1
        return f"DOC-{today}-{seq:03d}"

    # ── 切分逻辑 ─────────────────────────────────────────

    def _split_by_headings(self, text: str) -> list[tuple[str, str | None]]:
        """按 Markdown 标题切分，返回 [(正文, 章节标题), ...]"""
        sections = re.split(r"(^#{2,4}\s+.+$)", text, flags=re.MULTILINE)
        chunks: list[tuple[str, str | None]] = []
        current_title: str | None = None

        for part in sections:
            part_stripped = part.strip()
            if not part_stripped:
                continue
            if re.match(r"^#{2,4}\s+", part_stripped):
                current_title = re.sub(r"^#{2,4}\s+", "", part_stripped).strip()
            else:
                if current_title:
                    chunks.append((part_stripped, current_title))
                else:
                    chunks.append((part_stripped, None))
        return chunks

    def _split_by_clauses(self, text: str) -> list[str]:
        """按条款编号二次切分。"""
        parts = CLAUSE_PATTERN.split(text)
        result: list[str] = []
        buffer = ""

        for part in parts:
            if CLAUSE_PATTERN.match(part):
                if buffer.strip():
                    result.append(buffer.strip())
                buffer = part
            else:
                buffer += part

        if buffer.strip():
            result.append(buffer.strip())
        return result or [text]

    def _split_by_length(self, text: str, max_chars: int, overlap: int) -> list[str]:
        """按字符长度兜底切分，尽量在句末断。"""
        if len(text) <= max_chars:
            return [text]

        result: list[str] = []
        start = 0
        while start < len(text):
            end = min(start + max_chars, len(text))
            if end < len(text):
                # 回退到最近的句号/换行
                for sep in ("\n", "。", "；", "，", " "):
                    last = text.rfind(sep, start, end)
                    if last > start + max_chars // 2:
                        end = last + 1
                        break
            result.append(text[start:end].strip())
            start = end - overlap if end < len(text) else len(text)
        return result

    def _chunk_document(self, text: str) -> list[dict[str, Any]]:
        """三阶段切分：标题 → 条款 → 长度兜底。"""
        heading_sections = self._split_by_headings(text)
        if not heading_sections:
            heading_sections = [(text, None)]

        all_chunks: list[dict[str, Any]] = []
        for section_text, section_title in heading_sections:
            clause_parts = self._split_by_clauses(section_text)
            for clause_text in clause_parts:
                length_parts = self._split_by_length(
                    clause_text, MAX_CHUNK_CHARS, CHUNK_OVERLAP_CHARS
                )
                for part in length_parts:
                    all_chunks.append({
                        "content": part,
                        "section_title": section_title,
                        "token_count": len(part),  # 近似：字符数
                    })

        return all_chunks

    # ── 入库主流程 ───────────────────────────────────────

    def ingest(
        self,
        source_path: str,
        *,
        doc_name: str = "",
        session_id: str | None = None,
        batch_id: str | None = None,
        project_id: str | None = None,
        company_id: str | None = None,
    ) -> dict[str, Any]:
        """读取文档文件 → 切分 → 写入 SQLite + Chroma。PDF/Word 自动走 MinerU 解析。"""
        src = Path(source_path)
        if not src.exists():
            return {"error": f"文件不存在: {source_path}"}

        ext = src.suffix.lower()
        if ext not in {".md", ".txt", ".pdf", ".docx", ".doc"}:
            return {"error": f"不支持的格式：{ext}，支持 PDF / Word / Markdown / 纯文本"}

        # PDF/Word → MinerU 解析；.md/.txt → 直接读
        if ext in {".pdf", ".docx", ".doc"}:
            try:
                from service.mineru_parser import parse_with_mineru
                text = parse_with_mineru(src)
            except RuntimeError as exc:
                return {"error": f"MinerU 解析失败: {exc}"}
        else:
            text = src.read_text(encoding="utf-8").strip()
        if not text:
            return {"error": "文件内容为空"}

        if not doc_name:
            doc_name = src.name

        doc_id = self._generate_doc_id()
        now = datetime.now(timezone.utc).isoformat()

        # Step 1: 写入 documents 表
        with self._lock:
            conn = self._get_conn()
            conn.execute(
                """INSERT INTO documents (doc_id, doc_name, source_path, content_type,
                   status, session_id, batch_id, project_id, company_id,
                   chunk_count, char_count, created_at)
                   VALUES (?,?,?,?,?,'ingesting',?,?,?,?,0,?,?)""",
                (doc_id, doc_name, source_path, ext,
                 session_id, batch_id, project_id, company_id,
                 len(text), now),
            )
            conn.commit()
            conn.close()

        # Step 2: 切分
        try:
            chunks = self._chunk_document(text)
        except Exception as exc:
            self._mark_error(doc_id, f"切分失败: {exc}")
            return {"error": f"切分失败: {exc}"}

        if not chunks:
            self._mark_error(doc_id, "切分结果为空")
            return {"error": "切分结果为空"}

        # Step 3: 写入 SQLite chunks + Chroma
        try:
            chroma = self._get_vector_store()
            lc_docs: list[LCDocument] = []

            with self._lock:
                conn = self._get_conn()
                for idx, ch in enumerate(chunks):
                    chunk_id = f"{doc_id}-{idx:04d}"
                    conn.execute(
                        """INSERT INTO document_chunks
                           (chunk_id, doc_id, chunk_index, doc_name, content,
                            section_title, token_count, source_path,
                            session_id, batch_id, project_id, company_id)
                           VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                        (
                            chunk_id, doc_id, idx, doc_name, ch["content"],
                            ch.get("section_title"), ch["token_count"], source_path,
                            session_id, batch_id, project_id, company_id,
                        ),
                    )

                    metadata = {
                        "chunk_id": chunk_id,
                        "doc_id": doc_id,
                        "chunk_index": idx,
                        "doc_name": doc_name,
                        "section_title": ch.get("section_title") or "",
                        "source_path": source_path,
                    }
                    if session_id: metadata["session_id"] = session_id
                    if project_id: metadata["project_id"] = project_id

                    lc_docs.append(LCDocument(page_content=ch["content"], metadata=metadata))

                conn.execute(
                    "UPDATE documents SET status='ready', chunk_count=? WHERE doc_id=?",
                    (len(chunks), doc_id),
                )
                conn.commit()
                conn.close()

            # 写 Chroma
            if chroma and lc_docs:
                chroma.add_documents(lc_docs)

        except Exception as exc:
            self._mark_error(doc_id, f"写入失败: {exc}")
            return {"error": f"写入失败: {exc}"}

        logger.info("Document ingested: doc_id=%s chunks=%d", doc_id, len(chunks))
        return {
            "ok": True,
            "doc_id": doc_id,
            "doc_name": doc_name,
            "chunk_count": len(chunks),
            "char_count": len(text),
        }

    def _mark_error(self, doc_id: str, message: str) -> None:
        with self._lock:
            conn = self._get_conn()
            conn.execute(
                "UPDATE documents SET status='error', error_message=? WHERE doc_id=?",
                (message, doc_id),
            )
            conn.commit()
            conn.close()
        logger.error("Document ingest error: doc_id=%s error=%s", doc_id, message)

    # ── 检索 ─────────────────────────────────────────────

    def retrieve(
        self,
        query: str,
        *,
        doc_id: str | None = None,
        session_id: str | None = None,
        project_id: str | None = None,
        company_id: str | None = None,
        top_k: int = 5,
    ) -> list[dict[str, Any]]:
        """混合检索：Chroma 向量 + FTS5 全文 → RRF 融合。"""
        # 必须至少一个范围过滤
        if not any([doc_id, session_id, project_id, company_id]):
            return [{"error": "必须指定检索范围：doc_id / session_id / project_id / company_id 至少一个"}]

        dense_results = self._dense_search(query, top_k=20, doc_id=doc_id,
                                           session_id=session_id, project_id=project_id)
        fts_results = self._fts_search(query, top_k=20, doc_id=doc_id,
                                       session_id=session_id, project_id=project_id)
        return self._rrf_fusion(dense_results, fts_results, top_k=top_k)

    def _dense_search(
        self, query: str, top_k: int, **filters: str | None
    ) -> list[dict[str, Any]]:
        chroma = self._get_vector_store()
        if chroma is None:
            return []

        chroma_filter = {k: v for k, v in filters.items() if v is not None}
        try:
            docs_with_scores = chroma.similarity_search_with_score(
                query, k=top_k, filter=chroma_filter if chroma_filter else None
            )
        except Exception as exc:
            logger.warning("Chroma dense search failed: %s", exc)
            return []

        results: list[dict[str, Any]] = []
        for doc, score in docs_with_scores:
            results.append({
                "chunk_id": doc.metadata.get("chunk_id", ""),
                "doc_id": doc.metadata.get("doc_id", ""),
                "doc_name": doc.metadata.get("doc_name", ""),
                "content": doc.page_content,
                "section_title": doc.metadata.get("section_title", ""),
                "source_path": doc.metadata.get("source_path", ""),
                "chunk_index": doc.metadata.get("chunk_index", 0),
                "dense_score": float(score) if isinstance(score, (int, float)) else 0.0,
            })
        return results

    def _fts_search(
        self, query: str, top_k: int, **filters: str | None
    ) -> list[dict[str, Any]]:
        try:
            conn = self._get_conn()

            # 前置过滤条件直接打在 FTS5 表上，先过滤再 BM25 评分
            fts_where_parts = ["document_chunks_fts MATCH ?"]
            c_where_parts: list[str] = []
            params: list[Any] = [query]

            # doc_id / project_id 在 FTS5 表上有 UNINDEXED 列，优先前置过滤
            for fts_col in ("doc_id", "project_id"):
                val = filters.get(fts_col)
                if val:
                    fts_where_parts.append(f"f.{fts_col} = ?")
                    params.append(val)

            # session_id / company_id 只在 document_chunks 表
            for c_col in ("session_id", "company_id"):
                val = filters.get(c_col)
                if val:
                    c_where_parts.append(f"c.{c_col} = ?")
                    params.append(val)

            fts_where = " AND ".join(fts_where_parts)
            c_where = (" AND " + " AND ".join(c_where_parts)) if c_where_parts else ""

            sql = f"""
                SELECT c.chunk_id, c.doc_id, c.doc_name, c.content, c.section_title,
                       c.chunk_index, c.source_path, rank AS bm25_score
                FROM document_chunks_fts f
                JOIN document_chunks c ON f.rowid = c.rowid
                WHERE {fts_where}{c_where}
                ORDER BY rank
                LIMIT ?
            """
            params.append(top_k)
            rows = conn.execute(sql, params).fetchall()
            conn.close()
        except Exception as exc:
            logger.warning("FTS5 search failed: %s", exc)
            return []

        return [
            {
                "chunk_id": r["chunk_id"],
                "doc_id": r["doc_id"],
                "doc_name": r["doc_name"],
                "content": r["content"],
                "section_title": r["section_title"] or "",
                "chunk_index": r["chunk_index"],
                "source_path": r["source_path"],
                "keyword_score": float(r["bm25_score"]),
            }
            for r in rows
        ]

    def _rrf_fusion(
        self,
        dense: list[dict[str, Any]],
        keyword: list[dict[str, Any]],
        top_k: int = 5,
        k: int = 60,
    ) -> list[dict[str, Any]]:
        scores: dict[str, float] = {}
        meta: dict[str, dict[str, Any]] = {}

        for rank, item in enumerate(dense):
            cid = item["chunk_id"]
            scores[cid] = scores.get(cid, 0.0) + 1.0 / (k + rank + 1)
            if cid not in meta:
                meta[cid] = dict(item)

        for rank, item in enumerate(keyword):
            cid = item["chunk_id"]
            scores[cid] = scores.get(cid, 0.0) + 1.0 / (k + rank + 1)
            if cid not in meta:
                meta[cid] = dict(item)

        ranked = sorted(scores.items(), key=lambda x: x[1], reverse=True)[:top_k]
        results = []
        for cid, fused in ranked:
            entry = meta.get(cid, {})
            entry["fused_score"] = round(fused, 4)
            results.append(entry)
        return results

    # ── 文档列表查询 ─────────────────────────────────────

    def list_documents(
        self,
        *,
        session_id: str | None = None,
        project_id: str | None = None,
        status: str | None = "ready",
    ) -> list[dict[str, Any]]:
        conn = self._get_conn()
        where = ["1=1"]
        params: list[Any] = []
        if status:
            where.append("status = ?")
            params.append(status)
        if session_id:
            where.append("session_id = ?")
            params.append(session_id)
        if project_id:
            where.append("project_id = ?")
            params.append(project_id)

        rows = conn.execute(
            f"SELECT * FROM documents WHERE {' AND '.join(where)} ORDER BY created_at DESC",
            params,
        ).fetchall()
        conn.close()
        return [dict(r) for r in rows]


# 全局单例
document_indexer = DocumentIndexer()
