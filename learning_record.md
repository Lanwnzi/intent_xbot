# 合同审查系统 — 技术要点与优化记录

## 一、优化路径：从原型到生产

| 阶段 | 调用链 | LLM 调用次数 | 耗时（首次/缓存） |
|------|--------|:---------:|:-----------:|
| V1 原型 | 用户 → 主Agent → 子Agent → contract_review_tool → 内部LLM | **3-4 轮** | ~90s / ~90s |
| V2 子Agent | 用户 → 主Agent → contract_expert(路由) → contract_review(黑盒) | **2-3 轮** | ~60s / ~60s |
| V3 快速通道 | [合同审查]按钮 → POST /api/contracts/review (绕过Agent) | **1 轮** | ~35s / ~12s |

**关键手段**：快速通道 API 直接调 LLM 审核 + 代码模板渲染报告，绕过主Agent和子Agent的额外推理轮次。

---

## 二、核心技术要点

### 1. MinerU v3 API 响应结构适配

**问题**：MinerU v3 返回三层嵌套 `results.{filename}.md_content`，代码只查顶层 `result["md"]`。

**解决**：多重兜底解析链
```
优先: results.{filename}.md_content (v3)
次级: results.{filename}.content_list (JSON字符串需parse)
兜底: 顶层 md / markdown / content_list (旧版)
```

### 2. SHA256 内容缓存去重

**原理**：`sha256(file_content) → data/parsed/{hash}.md`，同文件秒级命中。

**效果**：第二次上传同一份合同，MinerU 耗时从 ~23s 降至 ~0.02s（缓存命中）。

### 3. 子Agent 路由器模式

**边界划分**：
```
子Agent（路由器 — 轻量）
  ├── 简单编排：list_documents → document_retrieval（RAG检索）
  └── 路由转发：contract_review（自包含黑盒，子Agent不参与审核逻辑）
```

**关键原则**：子Agent不亲自执行复杂业务。审核逻辑封闭在 contract_review_tool 内部。

### 4. ToolCallLimit 熔断

**问题**：MinerU 解析失败 → LLM 反复调 python_repl 尝试手动解析 → 死循环。

**解决**：LangChain 原生 `ToolCallLimitMiddleware(run_limit=10)`，单轮工具调用超10次自动终止。

### 5. FTS5 前置过滤优化

**优化前**：`MATCH query → 全库BM25评分 → JOIN过滤doc_id`
**优化后**：FTS5 表加 `doc_id UNINDEXED` 列，`f.doc_id = ?` 前置过滤，BM25 只评目标文档

```sql
CREATE VIRTUAL TABLE document_chunks_fts USING fts5(
    content, section_title, doc_name,
    doc_id UNINDEXED,      -- 不参与分词，只做等值过滤
    project_id UNINDEXED,
    content='document_chunks', content_rowid='rowid'
);
```

### 6. RRF 混合检索

**Chroma 向量**（语义匹配）+ **FTS5 BM25**（关键词匹配）→ RRF 融合排序。

**核心价值**：向量找"违约责任"≈"违约条款"（同义词），FTS5 找精确字面命中，融合后准确率高于任一种单独使用。

### 7. 快速通道绕过 Agent 链路

**流程对比**：
```
Agent 路径: 用户 → 主Agent(LLM) → 子Agent(LLM) → contract_review(LLM) → 3轮LLM
快速通道:  用户 → POST /api/contracts/review → 1次LLM审核 → 代码模板渲染报告
```

**报告渲染不调 LLM**：`_generate_report()` 是纯字符串拼接的模板引擎，零 LLM 调用。

### 8. 异步入库不阻塞审查

```python
# RAG 入库 fire-and-forget，不等 indexed 状态
threading.Thread(target=_ingest, daemon=True).start()
```

审查接口在报告保存完成后立即返回，入库在后台线程执行。

---

## 三、Core Files

| 文件 | 职责 |
|------|------|
| `api/contracts.py` | 快速通道 API，含耗时统计 Timings 类 |
| `service/mineru_parser.py` | 共享 MinerU 解析 + SHA256 缓存 |
| `service/document_indexer.py` | SQLite + Chroma + FTS5 全文索引 |
| `tools/contract_review_tool.py` | 自包含审核黑盒（MinerU → LLM → 报告 → 保存） |
| `tools/contract_sub_agent_tool.py` | 子Agent 包装，主Agent 通过此工具调用合同处理 |
| `tools/document_retrieval_tool.py` | 范围检索工具（强制 doc_id 过滤） |
| `graph/agent_factory.py` | Agent 工厂 + 子Agent prompt + 熔断中间件 |

---

## 四、架构图

```
主 Agent [terminal, python_repl, read_file, fetch_url, contract_expert]
  │
  ├── 通用工具（直接调用）
  │
  └── contract_expert（子Agent — 路由器）
        ├── contract_review ──── MinerU(缓存) → LLM审核(1次) → 模板渲染 → 报告
        ├── document_retrieval ── Chroma(向量) + FTS5(BM25) → RRF融合
        ├── list_documents
        └── list_contracts

[合同审查] 按钮 ──→ POST /api/contracts/review ──→ 绕过 Agent，1次LLM调用
[合同上传] 按钮 ──→ 上传 + 异步入库
手动输入        ──→ 主Agent → 子Agent → 工具（正常Agent链路）
```
