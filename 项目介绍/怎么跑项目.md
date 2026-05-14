# openclaw_chat_platform - 怎么跑这个项目

## 环境配置

### 技术栈和依赖列表

| 组件 | 要求 | 说明 |
|------|------|------|
| Python | 3.10+ | 后端运行环境 |
| Node.js | 18+ | 前端运行环境 |
| npm | latest | 前端包管理 |
| Postgres + pgvector | 可选 | 用于 Checkpointer 和向量存储 |
| Docker | 可选 | 用于 Langfuse + Postgres |

### 环境变量说明

在 `backend/config/.env` 中配置：

```env
# ===========================================
# LLM 配置（必须）
# ===========================================
LLM_PROVIDER=zhipu              # 可选: zhipu / bailian / deepseek / openai
LLM_MODEL=glm-5                 # 具体模型名
LLM_API_KEY=<your-api-key>      # 智谱 API Key
LLM_BASE_URL=https://open.bigmodel.cn/api/paas/v4/

# ===========================================
# Embedding 配置
# ===========================================
EMBEDDING_PROVIDER=bailian
EMBEDDING_MODEL=text-embedding-v4
EMBEDDING_API_KEY=<your-api-key>
EMBEDDING_BASE_URL=https://dashscope.aliyuncs.com/compatible-mode/v1

# ===========================================
# 记忆模块（可选，默认 off）
# ===========================================
MEMORY_BACKEND=v2               # off / v1 / v2
MEMORY_V2_INJECT=tool           # tool / always / off
MEMORY_V2_INJECT_TOP_K=3

# v2 高级配置
# MEMORY_V2_FUSION_METHOD=weighted_sum
# MEMORY_V2_DENSE_WEIGHT=0.3
# MEMORY_V2_KEYWORD_WEIGHT=0.7

# ===========================================
# Guardian 安全审查（可选）
# ===========================================
GUARDIAN_ENABLED=true           # true / false
GUARDIAN_PROVIDER=openai
GUARDIAN_MODEL=gpt-4.1-mini
GUARDIAN_TIMEOUT_MS=1500
GUARDIAN_FAIL_MODE=closed       # closed / open

# ===========================================
# Langfuse 追踪（可选）
# ===========================================
LANGFUSE_SECRET_KEY=<your-key>
LANGFUSE_PUBLIC_KEY=<your-key>
LANGFUSE_BASE_URL=http://localhost:3000
LANGFUSE_ENV=development

# ===========================================
# Postgres Checkpointer（可选）
# ===========================================
CHECKPOINTER=postgres           # memory / postgres
POSTGRES_DSN=postgresql://user:pass@localhost:5432/postgres
```

### 配置文件示例

完整配置参考 `backend/config/.env.example`。

---

## 运行步骤

### 1. 克隆项目

```bash
cd "C:\Users\lenovo\Desktop\agent推文\13_大模型agent项目_openclaw_chat_platform\openclaw_chat_platform"
```

### 2. 安装后端依赖

```bash
cd backend

# 创建虚拟环境（Windows）
python -m venv .venv
.venv\Scripts\activate

# 创建虚拟环境（Linux/Mac）
python -m venv .venv
source .venv/bin/activate

# 安装依赖
pip install -r requirements.txt
```

### 3. 配置环境变量

```bash
# 复制环境变量模板
copy config\.env.example config\.env

# 编辑 config\.env，填入你的 API Key
```

### 4. (可选) 启动 Docker 服务（Langfuse + Postgres）

如果需要完整的 Langfuse 追踪和 Postgres Checkpointer：

```bash
# 在 backend 目录下创建 docker-compose.yml 或使用项目已有的配置
docker compose up -d

# 验证服务是否启动
docker ps
```

### 5. 启动后端

```bash
cd backend
uvicorn app:app --host 0.0.0.0 --port 8002 --reload
```

### 6. 启动前端（新窗口）

```bash
cd frontend
npm install
npm run dev
```

### 7. 访问

浏览器打开 http://localhost:3000

---

## 数据查看

### API 端点

| 端点 | 方法 | 说明 |
|------|------|------|
| `/health` | GET | 健康检查 |
| `/api/chat` | POST | 对话（流式） |
| `/api/sessions` | GET | 会话列表 |
| `/api/sessions/{id}` | GET | 会话详情 |
| `/api/files` | GET | 文件列表 |
| `/api/config` | GET/PUT | 配置读写 |

### 会话存储位置

对话历史存储在 JSON 文件中：

```bash
# 查看所有会话
ls backend/sessions/

# 查看特定会话
cat backend/sessions/<session_id>.json
```

### 日志查看

```bash
# 后端日志（uvicorn 输出）
# 默认输出到 stdout，可在启动时重定向到文件

# Langfuse 日志（如果启用）
# 访问 http://localhost:3000 查看 Langfuse Dashboard
```

### 记忆索引查看

```bash
# v1 记忆（Chroma）
ls backend/memory_module_v1/storage/chroma_memory/

# v2 记忆（Postgres）
# 需要连接 Postgres 查看
psql $POSTGRES_DSN -c "SELECT * FROM distilled_objects LIMIT 10;"
```

---

## 典型运行流程

### 流程图

```
启动后端
    │
    ▼
加载 .env 配置
    │
    ▼
初始化 Checkpointer (InMemory / Postgres)
    │
    ▼
扫描 Skills → 生成 SKILLS_SNAPSHOT.md
    │
    ▼
初始化 AgentManager
    │
    ▼
注册所有 Tools (Terminal/PythonREPL/FetchURL/ReadFile/search_memory)
    │
    ▼
监听 :8002 端口
    │
    ├──────────────────────┐
    ▼                      ▼
用户打开浏览器            (等待请求)
http://localhost:3000
    │
    ▼
发送消息 → POST /api/chat
    │
    ▼
Agent 执行 → SSE 流式响应
    │
    ▼
前端渲染对话 → 用户看到回复
    │
    ▼
(v2) 后台蒸馏记忆
```

---

## 常见问题排查

### Q1: 启动报错 `ModuleNotFoundError`

**症状**：`python -m venv .venv` 成功但 `pip install` 报错

**解决**：
```bash
# 确保在正确的目录
cd backend

# 重新创建虚拟环境
rm -rf .venv
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
```

### Q2: API 返回 503 "Agent manager is not initialized"

**症状**：调用 `/api/chat` 返回 503

**原因**：后端启动时初始化失败

**解决**：
1. 检查 `.env` 配置是否正确
2. 检查 LLM API Key 是否有效
3. 查看 uvicorn 启动日志

### Q3: 消息发送后无响应

**症状**：前端显示发送成功但没有回复

**解决**：
1. 检查后端是否正常运行（`curl http://localhost:8002/health`）
2. 检查浏览器控制台是否有 JS 错误
3. 检查 `.env` 中 LLM 配置是否正确

### Q4: Langfuse 追踪看不到数据

**症状**：启用了 Langfuse 但 Dashboard 为空

**解决**：
1. 确认 Langfuse 服务已启动（`docker ps`）
2. 检查 `LANGFUSE_SECRET_KEY` 和 `LANGFUSE_PUBLIC_KEY` 是否正确
3. 检查 `LANGFUSE_BASE_URL` 是否指向正确的 Langfuse 实例

### Q5: v2 记忆检索返回空

**症状**：问及历史话题时 Agent 不知道

**解决**：
```bash
# 1. 确认会话已保存
ls backend/sessions/

# 2. 手动触发蒸馏
cd backend
python -m script.distill_all_sessions

# 3. 检查 Postgres 中的数据
psql $POSTGRES_DSN -c "SELECT COUNT(*) FROM distilled_objects;"
```

---

## 快速验证清单

```bash
# 1. 健康检查
curl http://localhost:8002/health

# 2. 测试对话（需要 session_id）
curl -X POST http://localhost:8002/api/chat \
  -H "Content-Type: application/json" \
  -d '{"message": "你好", "session_id": "test-001"}'

# 3. 查看会话列表
curl http://localhost:8002/api/sessions

# 4. 查看配置
curl http://localhost:8002/api/config
```
