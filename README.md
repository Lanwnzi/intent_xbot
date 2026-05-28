# Personal Agent Platform

> 一个本地优先、文件驱动、透明可控的 AI Agent 工作台 —— 支持多 LLM 后端、提示注入防御、双层长期记忆与可插拔技能系统。

[![Python](https://img.shields.io/badge/python-3.11+-blue.svg)](https://www.python.org/)
[![FastAPI](https://img.shields.io/badge/FastAPI-0.115+-009688.svg)](https://fastapi.tiangolo.com/)
[![Next.js](https://img.shields.io/badge/Next.js-14.2-black.svg)](https://nextjs.org/)
[![LangGraph](https://img.shields.io/badge/LangGraph-1.0+-green.svg)](https://www.langchain.com/langgraph)

---

## Project Introduction

OpenClaw Chat Platform 是一个面向开发者的本地化 AI Agent 对话平台。它不依赖单一 LLM 厂商，支持通过环境变量灵活切换智谱 GLM、阿里百炼 Qwen、DeepSeek、OpenAI 等后端。平台以 **文件驱动** 和 **透明可解释** 为核心理念：所有系统提示、技能定义、长期记忆都以本地 Markdown 文件存储，工具调用可回溯，对话上下文可压缩归档。

核心工作流基于 **LangGraph Agent**，在标准 `create_agent` 图基础上叠加了 **Guardian 安全中间件**（提示注入检测）和可选的 **SummarizationMiddleware**（长对话自动压缩）。项目配套了 Next.js + TypeScript 前端，提供 SSE 流式对话、工具调用回放、workspace 文件在线编辑等能力。

---

## Project Showcase

![OpenClaw 项目概览](show.png)

### 飞书机器人对话效果

![Feishu Bot Demo](feishu_demo.png)

---

## Features

### 多 LLM 后端统一接入
- 支持 **智谱 GLM**（glm-5）、**阿里百炼 Qwen**（qwen3.5-plus）、**DeepSeek**、**OpenAI** 及任何 OpenAI 兼容接口
- LLM / Embedding / Guardian 可分别选择不同 provider，API Key 完全独立
- 蒸馏（distillation）可选专用低成本模型以节省 token 开销

### Guardian 提示注入防御
- 在 agent 图入口 `before_agent` 阶段进行安全分类（安全 / 危险）
- 支持 `fail-open` 与 `fail-closed` 两种熔断策略
- 独立的 Guardian 模型与超时配置，可针对性选择轻量模型
- 认证失败、限流、超时等异常场景均映射到安全策略

### 双层长期记忆
- **v1（Chroma RAG）**：文件驱动的向量检索，`MEMORY.md` 变更自动重建索引
- **v2（结构化蒸馏）**：基于 PostgreSQL + pgvector 的 exchange 级蒸馏与混合检索
  - Dense（向量相似度）+ Keyword（BM25 / jieba 分词）双路融合
  - RRF 与 weighted_sum 两种融合策略可选
  - 支持 `tool`（agent 自主检索）和 `always`（每轮强制注入）两种注入模式
  - 每次对话结束后自动异步蒸馏，支持跨会话记忆搜索

### 对话管理
- 完整的 CRUD REST API（创建 / 列示 / 重命名 / 删除 / 获取历史）
- 长对话压缩归档（按 token 数总结，归档旧消息到 `archive/`）
- 自动生成中文会话标题
- 支持 PostgreSQL checkpointer 跨重启持久化对话状态

### 可插拔技能系统
- 基于 `skills/<name>/SKILL.md` 目录约定 + YAML frontmatter
- 内置 4 个技能：联网搜索（Tavily）、天气查询、本地知识库检索（kb-retriever）、失败恢复经验沉淀
- 自动生成 `SKILLS_SNAPSHOT.md` 快照注入系统提示

### 合同审查与文档 RAG 检索（新增）
- **子Agent 路由器架构**：contract_expert 子Agent 作为合同任务路由层，将审核（contract_review 黑盒）、文档检索（document_retrieval）、文件列表三路任务收敛至独立上下文，主Agent 仅负责意图路由与通用工具调度
- **合同审核链路**：集成 MinerU OCR 解析（SHA256 缓存去重，二次命中 ~0.02s）→ LLM 七维度结构化审核（合同主体/付款/交付/违约/解除/保密/争议）→ 代码模板渲染 Markdown 报告
- **快速通道 API**：`POST /api/contracts/review` 直连链路，绕过主Agent 与子Agent 的 2-3 轮 LLM 推理，端到端延迟从 ~90s 降至 ~12s（缓存命中）
- **文档 RAG 检索底座**：SQLite 元数据 + Chroma 稠密向量（bailian text-embedding-v4, 1024维）+ SQLite FTS5 BM25 关键词召回，RRF 融合排序。FTS5 表增加 doc_id/project_id UNINDEXED 前置过滤列，先过滤再评分
- **批量文档入库**：支持多文件并发处理，ThreadPoolExecutor(max_workers=3) 文档级并发。SHA256 三层去重（Redis → SQLite → 锁），SET NX 分布式锁防重复入库
- **Redis 热缓存加速**：`doc:index:{scope}:{hash}` 缓存入库状态，命中直接返回 ~1ms；`doc:batch:{batch_id}` 追踪批次进度；`doc:job:{job_id}` 记录单文件状态
- **SSE 实时进度推送**：`GET /documents/batch-events/{batch_id}` 每秒轮询 Redis 比对快照，变化时推送 batch_progress，终态自动关闭。EventSource 断连时自动 fallback 到 GET batch-status
- **入库状态分级**：ingesting → indexing → indexed / failed，Chroma 写入失败时自动 ids/where 补偿清理，SQLite chunks 保留为 retry 事实来源
- **递归标题切分**：自动检测最高标题层级，从该层开始递归：标题 → 条款 → 滑窗兜底。不超长就不下钻，短段落保留完整结构

### 工具调用熔断
- 基于 LangChain `ToolCallLimitMiddleware(run_limit=10)` 防止 LLM 工具调用死循环

### 可观测性
- 可选 Langfuse tracing 集成，自动记录 trace、token 用量
- Token 统计 API（基于 tiktoken cl100k_base 编码）
- 所有工具调用以 SSE 事件流输出，前端可回放

---

## Tech Stack

| Layer | Technology |
|---|---|
| **Agent Framework** | LangChain 1.0+ / LangGraph |
| **API Server** | FastAPI 0.115+ / Uvicorn |
| **LLM Providers** | ChatOpenAI (智谱/百炼/OpenAI 兼容), ChatTongyi, ChatDeepSeek |
| **Embedding** | DashScopeEmbeddings / OpenAIEmbeddings |
| **Checkpointer** | InMemorySaver / AsyncPostgresSaver |
| **Vector Store** | Chroma (v1), pgvector (v2) |
| **BM25 / Tokenizer** | rank-bm25 / jieba |
| **Database** | PostgreSQL 14+ (v2 memory + optional checkpointer) |
| **Frontend** | Next.js 14 / React 18 / TypeScript 5 / Tailwind CSS 3 |
| **Editor** | Monaco Editor (`@monaco-editor/react`) |
| **Streaming** | Server-Sent Events (SSE) |
| **Tracing** | Langfuse 2.0+ (optional) |
| **Testing** | pytest |
| **Web Search** | Tavily API |

---

## Project Structure

```
intent_xbot/
├── backend/
│   ├── app.py                         # FastAPI 入口 + lifespan 管理
│   ├── requirements.txt               # Python 依赖
│   ├── api/
│   │   ├── chat.py                    # /api/chat（SSE 流式对话）
│   │   ├── sessions.py                # /api/sessions（会话 CRUD）
│   │   ├── files.py                   # /api/files（workspace 文件读写）
│   │   ├── config_api.py              # /api/config/rag-mode
│   │   ├── compress.py                # /api/sessions/{id}/compress
│   │   └── tokens.py                  # /api/tokens（token 统计）
│   ├── graph/
│   │   ├── agent.py                   # AgentManager：astream, generate_title, summarize_history
│   │   ├── agent_factory.py           # AgentConfig + create_agent_from_config
│   │   ├── checkpointer.py            # InMemorySaver / AsyncPostgresSaver 单例
│   │   ├── guardian.py                # GuardianMiddleware + evaluate_guardian_input
│   │   ├── context.py                 # RequestContext + Langfuse callback 构建
│   │   └── llm.py                     # LLM / Embedding 工厂与注册表
│   ├── config/
│   │   ├── config.py                  # Settings + RuntimeConfigManager
│   │   ├── config.json                # 运行时配置持久化
│   │   └── .env.example              # 环境变量模板
│   ├── service/
│   │   ├── session_manager.py         # 会话 JSON 文件管理
│   │   ├── prompt_builder.py          # 系统提示组装（workspace + skills + memory hints）
│   │   └── memory_indexer.py          # v1 Chroma 向量索引管理
│   ├── tools/
│   │   ├── __init__.py                # get_all_tools 注册中心
│   │   ├── terminal_tool.py           # Shell 命令执行工具
│   │   ├── python_repl_tool.py        # Python 代码执行工具
│   │   ├── fetch_url_tool.py          # URL 抓取工具
│   │   ├── read_file_tool.py          # 本地文件读取工具
│   │   ├── search_knowledge_tool.py   # 知识库语义检索工具
│   │   └── skills_scanner.py          # SKILL.md 扫描 + 快照生成
│   ├── skills/
│   │   ├── SKILLS_SNAPSHOT.md         # 自动生成的技能列表快照
│   │   ├── web-search/                # 联网搜索（Tavily）
│   │   ├── get_weather/               # 天气查询（Open-Meteo / wttr.in）
│   │   ├── rag-skill/                 # 本地知识库渐进式检索
│   │   └── retry-lesson-capture/      # 失败恢复经验自动沉淀
│   ├── workspace/                     # Agent 人格与行为定义
│   │   ├── SOUL.md                    # 灵魂 / 行为准则
│   │   ├── IDENTITY.md                # 身份与风格
│   │   ├── AGENTS.md                  # Agent 使用指南（工具协议 + memory 协议）
│   │   └── USER.md                    # 用户画像与偏好
│   ├── memory_module_v1/              # ← 旧版长期记忆
│   │   ├── long_term_memory/MEMORY.md # 长期记忆文件
│   │   ├── sessions/                  # 会话 JSON 持久化
│   │   └── storage/chroma_memory/     # Chroma 向量持久化
│   ├── memory_module_v2/              # ← 新版结构化记忆
│   │   ├── service/
│   │   │   ├── api.py                 # distill_session / search_memory / get_exchange
│   │   │   ├── config.py              # 统一开关 + BM25 / 融合参数配置
│   │   │   └── ops.py                 # 增量检测、健康检查
│   │   ├── ingest/                    # 会话读取 + exchange 分段器
│   │   ├── distill/                   # LLM 蒸馏（exchange → structured object）
│   │   ├── retrieval/                 # dense / keyword / fusion / service
│   │   ├── storage/                   # PostgreSQL repos + schema.sql
│   │   ├── domain/                    # 数据模型 + 枚举
│   │   └── integrations/             # Agent 集成（middleware / tools）
│   ├── script/
│   │   ├── distill_all_sessions.py    # 全量蒸馏脚本
│   │   └── import_cursor_transcripts.py # Cursor 对话导入工具
│   └── tests/
│       ├── test_smoke.py
│       ├── test_guardian.py
│       └── test_agent_guardian_integration.py
├── frontend/
│   ├── next.config.mjs
│   ├── tailwind.config.ts
│   ├── tsconfig.json
│   ├── package.json
│   └── src/
│       ├── app/
│       │   ├── layout.tsx
│       │   ├── page.tsx                # 主页面（Sidebar + ChatPanel 布局）
│       │   └── globals.css
│       ├── components/
│       │   ├── chat/
│       │   │   ├── ChatPanel.tsx        # 对话面板
│       │   │   ├── ChatInput.tsx        # 输入框
│       │   │   ├── ChatMessage.tsx      # 消息气泡（含工具调用回放）
│       │   │   ├── ThoughtChain.tsx     # 思维链展示
│       │   │   └── RetrievalCard.tsx    # RAG 检索结果卡片
│       │   ├── editor/
│       │   │   └── InspectorPanel.tsx   # 文件编辑器（Monaco）
│       │   └── layout/
│       │       ├── Navbar.tsx           # 导航栏
│       │       ├── Sidebar.tsx          # 会话列表 + 功能菜单
│       │       └── ResizeHandle.tsx     # 拖拽调整面板宽度
│       └── lib/
│           ├── api.ts                   # API 客户端 + SSE 流解析
│           └── store.tsx                # React Context 全局状态管理
└── 项目介绍/                            # 项目文档（中文面试材料等）
```

---

## Installation

### 前置要求

- **Python 3.11+**
- **Node.js 18+**（推荐 20 LTS）
- **PostgreSQL 14+**（仅 memory v2 或 postgres checkpointer 时需要）
- **pgvector extension**（仅 memory v2 时需要）

### 1. 克隆仓库

```bash
git clone <repo-url>
cd intent_xbot
```

### 2. 安装后端依赖

```bash
cd backend
python -m venv .venv
# Linux/macOS:
source .venv/bin/activate
# Windows:
.venv\Scripts\activate

pip install -r requirements.txt
```

### 3. 安装前端依赖

```bash
cd frontend
npm install
```

### 4. 配置环境变量

```bash
cp backend/config/.env.example backend/config/.env
# 编辑 backend/config/.env，至少填入一个 LLM provider 的 API Key
```

### 5. 数据库初始化（可选 — 仅 memory v2 或 postgres checkpointer）

```bash
# 创建数据库
createdb openclaw_memory

# 执行 schema（memory v2）
psql -d openclaw_memory -f backend/memory_module_v2/storage/schema.sql
```

---

## Usage

### 启动后端

```bash
cd backend
uvicorn app:app --host 0.0.0.0 --port 8002 --reload
```

后端将在 `http://localhost:8002` 启动，提供以下端点：

| Method | Path | Description |
|---|---|---|
| `GET` | `/health` | 健康检查 |
| `POST` | `/api/chat` | SSE 流式对话 |
| `GET/POST` | `/api/sessions` | 会话列表 / 创建 |
| `PUT/DELETE` | `/api/sessions/{id}` | 重命名 / 删除会话 |
| `GET` | `/api/sessions/{id}/messages` | 获取会话消息 |
| `GET` | `/api/sessions/{id}/history` | 获取会话完整历史 |
| `POST` | `/api/sessions/{id}/compress` | 压缩会话历史 |
| `POST` | `/api/sessions/{id}/generate-title` | 生成会话标题 |
| `GET/POST` | `/api/files` | 读取 / 保存 workspace 文件 |
| `GET` | `/api/skills` | 列出所有技能 |
| `GET/PUT` | `/api/config/rag-mode` | 读写 RAG 模式状态 |
| `GET` | `/api/tokens/session/{id}` | 会话 token 统计 |
| `POST` | `/api/tokens/files` | 文件 token 统计 |

### 启动前端

```bash
cd frontend
npm run dev
```

前端将在 `http://localhost:7788` 启动，默认连接 `http://localhost:8002` 的后端 API。

### 快速验证

```bash
# 健康检查
curl http://localhost:8002/health

# 创建会话
curl -X POST http://localhost:8002/api/sessions \
  -H "Content-Type: application/json" \
  -d '{"title":"测试会话"}'

# 发送消息（需要已经启动后端）
curl -X POST http://localhost:8002/api/chat \
  -H "Content-Type: application/json" \
  -d '{"message":"你好","session_id":"<session_id>","stream":true}'
```

---

## Configuration

所有配置通过 `backend/config/.env` 环境变量管理。核心配置项：

### LLM

```bash
# Provider：zhipu / bailian / deepseek / openai（及别名）
LLM_PROVIDER=zhipu
LLM_MODEL=glm-5
LLM_API_KEY=your_key
LLM_BASE_URL=https://open.bigmodel.cn/api/paas/v4/
```

### Embedding

```bash
EMBEDDING_PROVIDER=bailian
EMBEDDING_MODEL=text-embedding-v4
EMBEDDING_API_KEY=your_key
```

### Guardian（提示注入防御）

```bash
GUARDIAN_ENABLED=true
GUARDIAN_PROVIDER=openai
GUARDIAN_MODEL=gpt-4.1-mini
GUARDIAN_API_KEY=your_key
GUARDIAN_TIMEOUT_MS=1500        # 超时毫秒数
GUARDIAN_FAIL_MODE=closed       # closed=超时即拦截 / open=超时即放行
```

### Checkpointer（对话状态持久化）

```bash
# 默认 InMemorySaver，以下启用 PostgreSQL：
CHECKPOINTER=postgres
POSTGRES_DSN=postgresql://user:password@localhost:5432/dbname
```

### Summarization（长对话压缩）

```bash
SUMMARIZATION_ENABLED=true
SUMMARIZATION_TRIGGER_MESSAGES=50   # 消息数达到此值触发压缩
SUMMARIZATION_KEEP_MESSAGES=20      # 保留最近 N 条消息
```

### Long-term Memory

```bash
# 统一开关：off（默认）/ v1（Chroma RAG）/ v2（结构化蒸馏）
MEMORY_BACKEND=v2

# v2 注入策略：tool（agent 自主调用）/ always（每轮强制注入）/ off（仅 API）
MEMORY_V2_INJECT=tool
MEMORY_V2_INJECT_TOP_K=3

# v2 融合策略：weighted_sum 或 rrf
MEMORY_V2_FUSION_METHOD=weighted_sum
MEMORY_V2_DENSE_WEIGHT=0.3
MEMORY_V2_KEYWORD_WEIGHT=0.7

# v2 专用蒸馏模型（可选，节省主 LLM token）
# DISTILL_PROVIDER=zhipu
# DISTILL_MODEL=glm-4-flash
```

### Langfuse Tracing（可选）

```bash
LANGFUSE_SECRET_KEY=sk-...
LANGFUSE_PUBLIC_KEY=pk-...
LANGFUSE_BASE_URL=https://cloud.langfuse.com
```

### Tavily Web Search（可选）

```bash
TAVILY_API_KEY=your_key
```

---

## Workflow / Architecture

### 请求处理流程

```
HTTP POST /api/chat
        │
        ▼
┌─────────────────┐
│  AgentManager   │
│  .astream()     │
└────────┬────────┘
         │
    ┌────▼────┐
    │ Memory  │  根据 MEMORY_BACKEND 选择注入策略：
    │ Inject  │  v1: Chroma RAG 检索 MEMORY.md
    │         │  v2: forced injection 或注册 search_memory tool
    └────┬────┘
         │
    ┌────▼────┐
    │ Guardian │  before_agent: 安全分类（安全/危险）
    │Middleware│  危险 → jump_to end, 返回拦截消息
    └────┬────┘
         │ 安全
    ┌────▼────┐
    │  Agent   │  LangGraph create_agent
    │  Graph   │  LLM + Tools + Checkpointer
    │          │  (+ optional SummarizationMiddleware)
    └────┬────┘
         │
    ┌────▼────┐
    │   SSE    │  流式输出: token / tool_start / tool_end / done
    │  Stream  │
    └────┬────┘
         │
    ┌────▼────┐
    │  Post-   │  保存消息 → 生成标题 → 异步蒸馏(v2)
    │ Process  │
    └─────────┘
```

### Memory v2 检索架构

```
search_memory(query)
        │
   ┌────▼─────┐
   │  Dense    │  pgvector cosine similarity (distilled objects)
   │  Search   │  → top_k=200 candidates
   └────┬─────┘
        │
   ┌────▼─────┐
   │  Keyword  │  BM25 + jieba (verbatim exchanges)
   │  Search   │  → top_k=200 candidates
   └────┬─────┘
        │
   ┌────▼─────┐
   │  Fusion   │  weighted_sum (default) or RRF
   │           │  → top_k=10 merged results
   └────┬─────┘
        │
   ┌────▼─────┐
   │  Response │  MemorySearchResponse + verbatim snippets
   │           │  + evidence drilldown via get_exchange()
   └───────────┘
```

### 系统提示组装

系统提示由 `prompt_builder.py` 按以下顺序合并：

1. 技能快照 `SKILLS_SNAPSHOT.md`（自动生成）
2. `workspace/SOUL.md`（行为准则）
3. `workspace/IDENTITY.md`（身份定义）
4. `workspace/USER.md`（用户画像）
5. `workspace/AGENTS.md`（工具使用协议）
6. 长期记忆提示（按 `MEMORY_BACKEND` 模式选择）

每个组件有字符数上限（`component_char_limit`，默认 20,000），超出自动截断。

---

## Example

### 基本对话

```python
# 使用 REST API 发起对话
import requests

# 1. 创建会话
resp = requests.post("http://localhost:8002/api/sessions", json={"title": "测试"})
session_id = resp.json()["id"]

# 2. 发送消息（SSE 流式）
response = requests.post(
    "http://localhost:8002/api/chat",
    json={"message": "用 Python 写一个快速排序", "session_id": session_id, "stream": True},
    stream=True
)

for line in response.iter_lines(decode_unicode=True):
    if line:
        print(line)
```

### 配置 memory v2

```bash
# .env 配置
MEMORY_BACKEND=v2
MEMORY_V2_INJECT=tool
POSTGRES_DSN=postgresql://localhost:5432/openclaw_memory
```

发送消息后，agent 可以自主调用 `search_memory` 工具检索历史对话；对话结束时会自动触发异步蒸馏。

### 切换 LLM Provider

```bash
# 从智谱切换到 DeepSeek
LLM_PROVIDER=deepseek
LLM_MODEL=deepseek-chat
LLM_API_KEY=sk-...
```

### 启用 Guardian 并查看拦截效果

```bash
GUARDIAN_ENABLED=true
GUARDIAN_FAIL_MODE=closed
```

当用户输入被判定为"危险"（提示注入等）时，agent 直接返回拦截消息，不进入 LLM 推理。

---

## Future Improvements

项目当前处于早期开发阶段，以下方向可供参考：

- [ ] **多模态支持**：扩展 Guardian 与 Agent，支持图片、文件等非文本输入的检查与处理
- [ ] **Auth / 多租户**：增加 API 鉴权与多用户隔离
- [ ] **Memory v2 Web UI**：在前端增加记忆检索结果的可视化面板与手动蒸馏入口
- [ ] **Tool 动态注册**：从 skills 目录自动发现工具，减少手动注册
- [ ] **Guardian 规则可配置**：允许用户通过 workspace 文件自定义安全策略与拦截规则
- [ ] **离线嵌入**：支持本地 embedding 模型，降低对云端 API 的依赖
- [ ] **Docker 化部署**：提供 `docker-compose.yml` 一键启动所有依赖
- [ ] **E2E 测试补充**：增加 API 集成测试与前端自动化测试
- [ ] **插件市场**：技能社区共享与一键安装机制

---

## License

（待补充）

---

## Contributing

欢迎提交 Issue 与 Pull Request。大型改动建议先讨论设计方案。

内部测试用例位于 `backend/tests/`，运行方式：

```bash
cd backend
pytest tests/ -v
```
