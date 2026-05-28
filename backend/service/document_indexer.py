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

MAX_CHUNK_CHARS = 1200
CHUNK_OVERLAP_CHARS = 120
CLAUSE_PATTERN = re.compile(
    r"(?:^|\n)(第[零一二三四五六七八九十百千\d]+[条章款节]|第\s*\d+\s*[条章款节]|\d+\.\s*\S)"
)
UPLOAD_PREFIX_PATTERN = re.compile(r"^[0-9a-fA-F]{12}_(.+)$")

# ── 状态流转: ingesting → indexing → indexed (可检索) / failed (不可用) ─

STATUS_INGESTING = "ingesting"
STATUS_INDEXING = "indexing"
STATUS_INDEXED = "indexed"
STATUS_FAILED = "failed"

# ── SQLite DDL（分三步：基础建表 → 迁移 → 索引）────────────────

DDL_BASE = """
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
    created_at   TEXT NOT NULL
);

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

CREATE INDEX IF NOT EXISTS idx_docs_status ON documents(status);
CREATE INDEX IF NOT EXISTS idx_docs_session ON documents(session_id);
CREATE INDEX IF NOT EXISTS idx_chunks_doc ON document_chunks(doc_id);
CREATE INDEX IF NOT EXISTS idx_chunks_project ON document_chunks(project_id);

-- FTS5: INSERT 由显式 executemany 驱动，DELETE 保留触发器清理
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

-- DELETE 触发器：chunk 删除时同步清理 FTS5
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

            # ① 基础建表（不依赖新增列的索引）
            conn.executescript(DDL_BASE)

            # ② 迁移：补齐旧表缺失的列
            existing_cols = {row[1] for row in conn.execute("PRAGMA table_info(documents)")}
            for col, col_type in [
                ("file_hash", "TEXT"),
                ("updated_at", "TEXT"),
                ("error_message", "TEXT"),
            ]:
                if col not in existing_cols:
                    conn.execute(f"ALTER TABLE documents ADD COLUMN {col} {col_type}")

            # ③ 旧 status='ready' 迁移为 'indexed'
            conn.execute("UPDATE documents SET status='indexed' WHERE status='ready'")

            # ④ 现在列已存在，安全创建依赖新列的索引
            for idx_sql in [
                "CREATE INDEX IF NOT EXISTS idx_docs_file_hash ON documents(file_hash)",
                "CREATE INDEX IF NOT EXISTS idx_docs_hash_status ON documents(file_hash, status, project_id)",
                (
                    "CREATE UNIQUE INDEX IF NOT EXISTS uq_docs_scope_hash_indexed "
                    "ON documents("
                    "file_hash, "
                    "COALESCE(project_id, ''), "
                    "COALESCE(session_id, ''), "
                    "COALESCE(company_id, '')"
                    ") "
                    "WHERE status = 'indexed'"
                ),
            ]:
                try:
                    conn.execute(idx_sql)
                except sqlite3.IntegrityError as exc:
                    if "uq_docs_scope_hash_indexed" in idx_sql:
                        logger.warning(
                            "[document_indexer] skip unique index uq_docs_scope_hash_indexed due to existing duplicates: %s",
                            exc,
                        )
                    else:
                        raise

            # ⑤ FTS5 表被 DROP+CREATE 重建后，已有 chunks 需重新同步（只做一次）
            count = conn.execute("SELECT COUNT(*) AS cnt FROM document_chunks_fts").fetchone()[0]
            if count == 0:
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

    def _normalize_doc_name(self, doc_name: str) -> str:
        match = UPLOAD_PREFIX_PATTERN.match(doc_name)
        if match:
            return match.group(1)
        return doc_name

    def _now_iso(self) -> str:
        return datetime.now(timezone.utc).isoformat()

    # ── 切分逻辑 ────────────────────────────────────────

    @staticmethod
    def _heading_level(line: str) -> int | None:
        """返回 heading 层级（2-6），非标题返回 None。"""
        stripped = line.lstrip()
        if not stripped.startswith("#"):
            return None
        level = len(stripped) - len(stripped.lstrip("#"))
        if level < 1 or level > 6:
            return None
        if len(stripped) <= level or not stripped[level].isspace():
            return None
        return level

    @staticmethod
    def _heading_text(line: str) -> str:
        stripped = line.lstrip()
        level = DocumentIndexer._heading_level(stripped)
        if level is None:
            return ""
        return stripped[level:].strip()

    def _split_by_headings(self, text: str, target_level: int) -> list[tuple[str, str | None]]:
        """按指定层级标题切分，返回 [(正文, 章节标题)]。"""
        sections: list[tuple[str, str | None]] = []
        current_lines: list[str] = []
        current_title: str | None = None

        for line in text.splitlines():
            level = self._heading_level(line)
            if level is not None and level == target_level:
                if current_lines:
                    sections.append(("\n".join(current_lines).strip(), current_title))
                current_lines = [line]
                current_title = self._heading_text(line)
            else:
                current_lines.append(line)

        if current_lines:
            sections.append(("\n".join(current_lines).strip(), current_title))
        return [(content, title) for content, title in sections if content]

    def _split_by_clauses(self, text: str) -> list[str]:
        """按条款编号（第X条/第X章）切分。"""
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
        """按字符长度滑窗切分，尽量在句末断。"""
        if len(text) <= max_chars:
            return [text]
        result: list[str] = []
        start = 0
        while start < len(text):
            end = min(start + max_chars, len(text))
            if end < len(text):
                for sep in ("\n\n", "\n", "。", "；", "，"):
                    last = text.rfind(sep, start, end)
                    if last > start + max_chars // 2:
                        end = last + len(sep)
                        break
            result.append(text[start:end].strip())
            start = end - overlap if end < len(text) else len(text)
        return result

    def _recursive_chunk(
        self, text: str, title: str | None, level: int
    ) -> list[dict[str, Any]]:
        """递归切分：标题层级 → 条款 → 滑窗兜底。

        从指定层级标题开始切。每段不超长就保留，超长才下钻到下一级。
        """
        text = text.strip()
        if not text:
            return []

        # 不超长 → 直接保留
        if len(text) <= MAX_CHUNK_CHARS:
            return [{"content": text, "section_title": title, "token_count": len(text)}]

        # 还有更低级标题可切 → 下钻
        if level <= 5:
            sub_sections = self._split_by_headings(text, target_level=level + 1)
            if len(sub_sections) >= 2:  # 确实切出了多个子段
                chunks: list[dict[str, Any]] = []
                for sub_text, sub_title in sub_sections:
                    chunks.extend(self._recursive_chunk(sub_text, sub_title or title, level + 1))
                return chunks

        # 标题不够用 → 条款切分
        if level <= 6:  # 对任意层级都可以尝试条款
            clause_parts = self._split_by_clauses(text)
            if len(clause_parts) >= 2:
                chunks = []
                for clause in clause_parts:
                    if len(clause) <= MAX_CHUNK_CHARS:
                        chunks.append({"content": clause, "section_title": title, "token_count": len(clause)})
                    else:
                        chunks.extend([
                            {"content": p, "section_title": title, "token_count": len(p)}
                            for p in self._split_by_length(clause, MAX_CHUNK_CHARS, CHUNK_OVERLAP_CHARS)
                        ])
                return chunks

        # 最终兜底：滑窗切分
        return [
            {"content": p, "section_title": title, "token_count": len(p)}
            for p in self._split_by_length(text, MAX_CHUNK_CHARS, CHUNK_OVERLAP_CHARS)
        ]

    def _detect_top_heading_level(self, text: str) -> int:
        """找到文本中最高标题层级，没有标题返回 6。"""
        min_level = 6
        for line in text.splitlines():
            level = self._heading_level(line)
            if level is not None and level < min_level:
                min_level = level
        return min_level

    def _chunk_document(self, text: str) -> list[dict[str, Any]]:
        """入口：检测最高标题层级，从该层开始递归切分。"""
        text = text.strip()
        if not text:
            return []

        top_level = self._detect_top_heading_level(text)
        top_sections = self._split_by_headings(text, target_level=top_level)
        if len(top_sections) <= 1:
            # 只有一个顶层块或无标题 → 直接递归
            return self._recursive_chunk(text, None, top_level)

        chunks: list[dict[str, Any]] = []
        for section_text, section_title in top_sections:
            chunks.extend(self._recursive_chunk(section_text, section_title, top_level))
        return chunks

    # ── 状态标记 ─────────────────────────────────────────

    def _mark_failed(self, doc_id: str, message: str) -> None:
        with self._lock:
            conn = self._get_conn()
            conn.execute(
                "UPDATE documents SET status=?, error_message=?, updated_at=? WHERE doc_id=?",
                (STATUS_FAILED, message, self._now_iso(), doc_id),
            )
            conn.commit()
            conn.close()
        logger.error("[ingest] doc_id=%s FAILED: %s", doc_id, message)

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
        """读取文档文件 → 切分 → 批量写入 SQLite + Chroma。"""
        src = Path(source_path)

        # ── 0. 前置校验（无 doc_id） ──
        if not src.exists():
            return {"ok": False, "error": f"文件不存在: {source_path}"}
        ext = src.suffix.lower()
        if ext not in {".md", ".txt", ".pdf", ".docx", ".doc"}:
            return {"ok": False, "error": f"不支持的格式：{ext}，支持 PDF / Word / Markdown / 纯文本"}
        if not doc_name:
            doc_name = src.name
        doc_name = self._normalize_doc_name(doc_name)

        # ── 1. 读取文件 + 生成 doc_id ──
        file_hash: str
        text: str
        try:
            if ext in {".pdf", ".docx", ".doc", ".md", ".txt"}:
                from service.document_parser import parse_uploaded_file
                from service.mineru_parser import compute_file_hash
                file_hash = compute_file_hash(src)
                text = parse_uploaded_file(src, file_hash=file_hash)
            else:
                from hashlib import sha256
                file_hash = sha256(src.read_bytes()).hexdigest()
                text = src.read_text(encoding="utf-8").strip()
        except RuntimeError as exc:
            return {"ok": False, "error": f"文件读取/解析失败: {exc}"}
        except Exception as exc:
            logger.exception("[ingest] file read error")
            return {"ok": False, "error": f"文件读取失败: {exc}"}

        if not text:
            return {"ok": False, "error": "文件内容为空"}

        # doc_id 在此生成 — 文件存在且可读后
        doc_id = self._generate_doc_id()
        now = self._now_iso()

        # ── 2. INSERT documents (status='ingesting') ──
        with self._lock:
            conn = self._get_conn()

            # 去重：同一 hash 且 scope 下已有 indexed 文档
            existing = conn.execute(
                """SELECT doc_id FROM documents
                   WHERE file_hash = ? AND status = ?
                     AND (? IS NULL OR project_id = ?)
                     AND (? IS NULL OR session_id = ?)
                   LIMIT 1""",
                (file_hash, STATUS_INDEXED, project_id, project_id, session_id, session_id),
            ).fetchone()
            if existing:
                conn.close()
                logger.info("[ingest] dedup hit: hash=%s existing=%s", file_hash[:12], existing["doc_id"])
                return {"ok": True, "doc_id": existing["doc_id"], "status": STATUS_INDEXED}

            conn.execute(
                """INSERT INTO documents (doc_id, doc_name, source_path, content_type,
                   status, session_id, batch_id, project_id, company_id,
                   file_hash, chunk_count, char_count, created_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,0,?,?)""",
                (doc_id, doc_name, source_path, ext,
                 STATUS_INGESTING, session_id, batch_id, project_id, company_id,
                 file_hash, len(text), now,  # 12个tuple值,匹配12个?: 10个字段+0(chunk_count)+char_count+created_at
                ),
            )
            conn.commit()
            conn.close()

        # ── 3. 切分 ──
        try:
            chunks = self._chunk_document(text)
        except Exception as exc:
            self._mark_failed(doc_id, f"切分失败: {exc}")
            return {"ok": False, "doc_id": doc_id, "status": STATUS_FAILED, "error": f"切分失败: {exc}"}

        if not chunks:
            self._mark_failed(doc_id, "文档切分结果为空")
            return {"ok": False, "doc_id": doc_id, "status": STATUS_FAILED, "error": "文档切分结果为空"}

        # ── 4. 构建批量写入数据 ──
        chunk_rows: list[tuple] = []
        chunk_ids: list[str] = []
        chroma_contents: list[str] = []
        chroma_metadatas: list[dict] = []
        base_meta = {
            "doc_id": doc_id, "doc_name": doc_name, "source_path": source_path,
        }
        if session_id:
            base_meta["session_id"] = session_id
        if project_id:
            base_meta["project_id"] = project_id

        for idx, ch in enumerate(chunks):
            cid = f"{doc_id}-{idx:04d}"
            chunk_ids.append(cid)
            chunk_rows.append((
                cid, doc_id, idx, doc_name, ch["content"],
                ch.get("section_title"), ch["token_count"], source_path,
                session_id, batch_id, project_id, company_id,
            ))
            meta = {
                **base_meta,
                "chunk_id": cid,
                "chunk_index": idx,
                "section_title": ch.get("section_title") or "",
            }
            chroma_contents.append(ch["content"])
            chroma_metadatas.append(meta)

        # ── 5. SQLite 事务: document_chunks + FTS5 ──
        try:
            with self._lock:
                conn = self._get_conn()
                try:
                    conn.execute("BEGIN")
                    conn.executemany(
                        """INSERT INTO document_chunks
                           (chunk_id, doc_id, chunk_index, doc_name, content,
                            section_title, token_count, source_path,
                            session_id, batch_id, project_id, company_id)
                           VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                        chunk_rows,
                    )
                    # 显式批量写 FTS5（不依赖触发器）
                    conn.executemany(
                        """INSERT INTO document_chunks_fts
                           (rowid, content, section_title, doc_name, doc_id, project_id)
                           SELECT rowid, content, section_title, doc_name, doc_id, project_id
                           FROM document_chunks WHERE chunk_id = ?""",
                        [(cid,) for cid in chunk_ids],
                    )
                    conn.execute(
                        "UPDATE documents SET status=?, chunk_count=?, updated_at=? WHERE doc_id=?",
                        (STATUS_INDEXING, len(chunks), self._now_iso(), doc_id),
                    )
                    conn.execute("COMMIT")
                except Exception:
                    conn.execute("ROLLBACK")
                    raise
                finally:
                    conn.close()
        except Exception as exc:
            self._mark_failed(doc_id, f"SQLite 写入失败: {exc}")
            return {"ok": False, "doc_id": doc_id, "status": STATUS_FAILED, "error": f"SQLite 写入失败: {exc}"}

        # ── 6. Chroma 批量写入（使用稳定 chunk_id） ──
        cleanup_error: str | None = None
        try:
            chroma = self._get_vector_store()
            if chroma:
                chroma.add_texts(texts=chroma_contents, metadatas=chroma_metadatas, ids=chunk_ids)
        except Exception as exc:
            # 补偿清理
            try:
                chroma.delete(ids=chunk_ids)
            except Exception:
                try:
                    chroma.delete(where={"doc_id": doc_id})
                except Exception as ce:
                    cleanup_error = str(ce)
                    logger.error("[ingest] doc_id=%s Chroma cleanup failed: %s", doc_id, ce)

            self._mark_failed(doc_id, f"Chroma 写入失败: {exc}")
            result: dict = {
                "ok": False, "doc_id": doc_id, "status": STATUS_FAILED,
                "error": f"Chroma 写入失败: {exc}",
            }
            if cleanup_error:
                result["cleanup_error"] = cleanup_error
            return result

        # ── 7. 更新 status='indexed' ──
        try:
            with self._lock:
                conn = self._get_conn()
                conn.execute(
                    "UPDATE documents SET status=?, chunk_count=?, updated_at=?, error_message=NULL WHERE doc_id=?",
                    (STATUS_INDEXED, len(chunks), self._now_iso(), doc_id),
                )
                conn.commit()
                conn.close()
        except Exception as exc:
            logger.error("[ingest] doc_id=%s status update to indexed failed: %s", doc_id, exc)
            return {
                "ok": False, "doc_id": doc_id, "status": STATUS_INDEXING,
                "error": f"状态更新失败（数据已写入但 status 保持 indexing）: {exc}",
            }

        logger.info("[ingest] done: doc_id=%s chunks=%d", doc_id, len(chunks))
        return {
            "ok": True, "doc_id": doc_id, "status": STATUS_INDEXED,
            "chunk_count": len(chunks), "char_count": len(text),
        }

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
        """混合检索：Chroma 向量 + FTS5 全文 → RRF 融合。只返回 status='indexed' 文档的 chunks。"""
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
            logger.warning("[dense] search failed: %s", exc)
            return []

        # 收集所有涉及的 doc_id，批量检查其 status
        doc_ids = list({doc.metadata.get("doc_id", "") for doc, _ in docs_with_scores if doc.metadata.get("doc_id")})
        indexed_docs: set[str] = set()
        if doc_ids:
            conn = self._get_conn()
            rows = conn.execute(
                f"SELECT doc_id FROM documents WHERE doc_id IN ({','.join(['?']*len(doc_ids))}) AND status = ?",
                (*doc_ids, STATUS_INDEXED),
            ).fetchall()
            conn.close()
            indexed_docs = {r["doc_id"] for r in rows}

        results: list[dict[str, Any]] = []
        for doc, score in docs_with_scores:
            d_id = doc.metadata.get("doc_id", "")
            if d_id not in indexed_docs:
                continue  # 过滤非 indexed 文档
            results.append({
                "chunk_id": doc.metadata.get("chunk_id", ""),
                "doc_id": d_id,
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

            fts_where_parts = ["document_chunks_fts MATCH ?"]
            c_where_parts = ["d.status = ?"]
            params: list[Any] = [query, STATUS_INDEXED]

            for fts_col in ("doc_id", "project_id"):
                val = filters.get(fts_col)
                if val:
                    fts_where_parts.append(f"f.{fts_col} = ?")
                    params.append(val)

            for c_col in ("session_id", "company_id"):
                val = filters.get(c_col)
                if val:
                    c_where_parts.append(f"c.{c_col} = ?")
                    params.append(val)

            fts_where = " AND ".join(fts_where_parts)
            c_where = " AND ".join(c_where_parts)

            sql = f"""
                SELECT c.chunk_id, c.doc_id, c.doc_name, c.content, c.section_title,
                       c.chunk_index, c.source_path, rank AS bm25_score
                FROM document_chunks_fts f
                JOIN document_chunks c ON f.rowid = c.rowid
                JOIN documents d ON c.doc_id = d.doc_id
                WHERE {fts_where} AND {c_where}
                ORDER BY rank
                LIMIT ?
            """
            params.append(top_k)
            rows = conn.execute(sql, params).fetchall()
            conn.close()
        except Exception as exc:
            logger.warning("[fts] search failed: %s", exc)
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
        status: str | None = STATUS_INDEXED,
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
        results: list[dict[str, Any]] = []
        for row in rows:
            item = dict(row)
            item["doc_name"] = self._normalize_doc_name(str(item.get("doc_name", "")))
            results.append(item)
        return results


# 全局单例
document_indexer = DocumentIndexer()
