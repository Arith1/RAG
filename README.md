# RAG 个人知识库

基于**文件指纹增量同步 + MySQL/Milvus 双库协作**的 RAG 问答系统：文档入库自动判定同名同源与内容变化（未变跳过 / 变更增量更新 / 失败可重试），检索侧双路召回 + 重排精排，接入 Agent 生成带来源引用的回答；配套完整 Web 服务（JWT + RBAC）、会话记忆（Postgres 持久化）、可靠入库任务队列（Redis Streams）。

> 核心亮点：**四存储各司其职**——MySQL（元数据权威）、Milvus（向量检索）、Postgres（对话记忆）、Redis（任务队列/限流/缓存索引）；两阶段落库 + 状态机保障跨库一致性，杜绝孤儿向量；上传/删除/账户删除等写操作依赖 Redis Streams 保证任务不丢，检索问答在依赖不可用时尽量降级。

---

## 功能特性

- **文件指纹增量入库**：`identity_hash` 判定同名同源，`file_content_hash` 判定内容变化——skip / insert / update / retry 四态分流，只增量更新差集。
- **跨库一致性**：MySQL 先落期望状态（`pending`）→ Milvus 幂等同步 → `in_sync`/`failed` 状态机；失败重试按 source 重建，可清理孤儿向量。
- **结构感知切分**：Markdown 标题分节、**表格/公式/问答对原子保护**（占位符 + 还原长度分组）、超长表格自动拆子表并重复表头、图片路径入 metadata。
- **高质量检索**：dense（bge-m3）+ BM25（jieba 中文分词）双路召回 → RRF 融合 → bge-reranker-v2-m3 精排 → 阈值过滤；支持 `source` / 原生表达式过滤；**检索结果 / embedding / 回答三层 Redis 缓存**（相同问题秒回、省 API 调用）。
- **Agent 问答**：意图识别（规则层 + LLM 查询重构，多轮指代补全）→ 检索 → DeepSeek Agent 生成带来源引用回答；对话记忆按用户隔离。
- **会话记忆**：Postgres 持久化（跨重启/多 worker），TTL 按"最后活跃时间"自动清理，~20 轮对话自动摘要压缩。
- **可靠入库队列**：Redis Streams（Consumer Group + PEL 崩溃恢复、持久化延迟重试、死信队列、inflight 竞态防护 409）。
- **可靠删除队列**：账户删除同样走 Redis Streams，按 Milvus → OSS → 本地文件 → MySQL → 缓存清理顺序执行，失败持久化重试，不卡账号。
- **权限模型**：普通用户可上传/删除自己的文档；管理员可把共享文档取消为私有；下载仅 owner 或共享文档可访问。
- **多问题问答**：意图识别支持拆分多个子问题，逐个检索后汇总分点回答，并限制最大子问题数。
- **精准缓存失效**：维护 `src_idx:{source}` 缓存索引，文档取消共享/删除/账户删除时 O(1) 定位清理相关缓存。
- **Web 服务**：FastAPI + JWT 认证 + RBAC + 操作审计 + 登录/注册失败限流 + 路径脱敏。
- **可度量**：25 题 golden 评测集（hit@3 = 100%）、分层检索对比实验、52 个 pytest 用例。

## 系统架构

```mermaid
flowchart LR
    subgraph Web["FastAPI 服务"]
        A["/chat 问答页<br/>/api/*"]
        B[认证 JWT + RBAC]
        C[文档管理<br/>上传/删除/列表]
        D[问答编排<br/>意图识别→检索→Agent]
    end

    subgraph 入库管线
        E[文件] --> F{复杂度评估}
        F -- 简单 --> G[Unstructured 本地解析]
        F -- 复杂 --> H[MinerU 云端解析]
        G & H --> I[结构感知切分]
        I --> J[Redis Streams 队列]
        J --> K[worker: 两阶段落库]
        K --> L[(MySQL 元数据)]
        K --> M[(Milvus 向量)]
    end

    subgraph 检索问答
        Q[问题] --> D
        D --> N[双路召回 dense+BM25]
        N --> O[RRF + rerank 精排]
        O --> P[阈值过滤 + 来源引用]
        P --> D
    end

    D --> R[(Postgres 对话记忆)]
    C --> S[(Redis: ingest 队列/限流)]
    L --> T[状态机 in_sync/failed]
```

**同步状态机**

```mermaid
stateDiagram-v2
    [*] --> pending: 入库 / 更新落库
    pending --> in_sync: Milvus 同步成功
    pending --> failed: Milvus 同步失败
    failed --> pending: 重跑重放（按 source 重建）
    in_sync --> pending: 文件内容变更
```

## 技术栈

| 层 | 选型 |
| --- | --- |
| 语言 / 运行时 | Python 3.14+ |
| Web 框架 | FastAPI + uvicorn |
| 前端 | Vue 3 + Vite + TypeScript + Pinia + Vue Router（SSE 流式问答） |
| 元数据存储 | MySQL 8.x + SQLAlchemy 2.0（async）+ aiomysql |
| 向量存储 | Milvus 2.x + pymilvus + langchain-milvus |
| 对话记忆 | Postgres（langgraph-checkpoint-postgres）+ TTL 清理 |
| 任务队列 / 限流 / 缓存 | Redis 8 + redis-py（Streams / ZSET / KV 缓存） |
| Embedding / Rerank | SiliconFlow：`BAAI/bge-m3`、`BAAI/bge-reranker-v2-m3` |
| 文档解析 | MinerU（复杂文档）+ Unstructured（简单文档）+ python-docx |
| LLM / Agent | DeepSeek + langchain 1.x（create_agent / LangGraph checkpointer） |
| 测试 | pytest（52 用例） |

## 快速开始

### 一键启动（Docker Compose，推荐）

```bash
# 1. 配置密钥（复制模板后填写 SILICONFLOW_API_KEY / MINERU_API_TOKEN /
#    DEEPSEEK_API_KEY / JWT_SECRET / ADMIN_PASSWORD 等）
cp .env.example .env

# 2. 一键启动全部服务（MySQL / Milvus(含 etcd+minio) / Redis / Postgres / API）
docker compose -f docker/docker-compose.yml up -d --build

# 3. 首次问答后，为 Postgres 对话记忆补充 created_at 列（幂等，TTL 清理的时间依据）
docker compose exec postgres psql -U root -d rag-demo -f /init-sql/postgres_memory.sql

# 4. 访问
#    问答页  http://localhost:8010/chat      Swagger: http://localhost:8010/docs
```

- 数据持久化：容器删除不丢数据（compose 数据卷）；文档/上传产物落在宿主机 `rag个人知识库/resources/`
- 常用命令：`docker compose down`（停）、`docker compose logs -f api`（看日志）
- 初始化：MySQL 首次启动自动建 4 张业务表（`docker/init/` + `models/vector.sql`）

### 手动启动（不用 compose）

**环境要求**：Python 3.14+、MySQL 8.x、Milvus 2.x、Redis 7+、Postgres 16+。

```bash
# 启动四个依赖服务（已有可跳过）
docker run -d --name milvus -p 19530:19530 -p 9091:9091 milvusdb/milvus:v2.5.4 standalone
docker run -d --name redis  -p 6379:6379 redis:7
docker run -d --name pg-mem -e POSTGRES_USER=root -e POSTGRES_PASSWORD=root \
  -e POSTGRES_DB=rag-demo -p 5432:5432 postgres:16
# MySQL 请自行准备（本机安装或容器）
```

### 安装

```bash
git clone https://github.com/Arith1/RAG.git
cd rag_project
uv sync
cp .env.example .env   # 填写各密钥（见下表）
```

`.env` 必需配置项：

| 变量 | 说明 |
| --- | --- |
| `ASYNC_DATABASE_URL` | MySQL 连接串，如 `mysql+aiomysql://root:root@localhost:3306/rag_demo?charset=utf8` |
| `SILICONFLOW_API_KEY` | SiliconFlow 密钥（embedding + rerank） |
| `MINERU_API_TOKEN` | MinerU 解析服务 Token（复杂 Word/PDF） |
| `MILVUS_URI` | Milvus 地址，默认 `http://localhost:19530` |
| `MEMORY_DATABASE_URL` | Postgres 连接串（对话记忆），如 `postgresql://root:root@localhost:5432/rag-demo` |
| `REDIS_URL` | Redis 连接串，默认 `redis://localhost:6379/0` |
| `DEEPSEEK_API_KEY` | DeepSeek 密钥（问答生成） |
| `JWT_SECRET` | JWT 签名密钥（必填，且长度至少 32 位） |
| `ADMIN_USERNAME` / `ADMIN_PASSWORD` | 首次启动播种的管理员账号 |

### 初始化数据库

```bash
# 1. MySQL 业务表（users / vector_files / chunk_records / audit_logs）
mysql -uroot -p -e "CREATE DATABASE rag_demo CHARACTER SET utf8mb4;"
mysql -uroot -p rag_demo < rag个人知识库/models/vector.sql

# 2. Postgres 对话记忆：langgraph 首次使用自动建 checkpoint 表；
#    再执行一次补充 created_at 列（TTL 清理的时间依据，幂等）
psql -U root -d rag-demo -f rag个人知识库/models/postgres_memory.sql
```

> 表结构采用**手动 SQL 管理**（`models/*.sql`），代码不做任何 DDL，改表后需同步 SQL 文件并手动执行。

### 启动服务

```bash
uvicorn rag个人知识库.api.main:app --host 0.0.0.0 --port 8010
```

- **问答页面**：http://localhost:8010/chat （内置 HTML 问答页，登录后提问）
- **Swagger 文档**：http://localhost:8010/docs
- **队列状态**：`GET /api/ingest/stats`

启动日志确认：`Redis 可用 → 入库任务队列已启用`、`对话记忆已启用 Postgres 持久化`（任一不可用会自动降级并提示）。

### 启动 Vue 前端（可选，标准前端）

```bash
cd rag_frontend
npm install          # 首次
npm run dev          # http://localhost:5173（/api 自动代理到 8010）
npm run build        # 类型检查 + 产物构建
```

前端功能：登录（JWT）、**SSE 流式问答**（打字机效果 + 来源引用）、文档管理（登录用户上传/删除自己的文档 + 队列状态）。

### 入库文档（API 上传）

```bash
# 登录用户通过 API 上传（走 Redis 队列）
curl -X POST http://localhost:8010/api/documents/upload \
  -H "Authorization: Bearer <token>" -F "file=@path/to/doc.md"
```

输出示例：`全新入库 v1.0` / `内容变更，已更新（新增 3 / 未变 20 / 删除 1）` / `内容未变，已跳过`。

### 文档入库约定（重要）

系统采用**"人工轻约定 + 程序自动转换"**的分层策略，不需要精细排版。人工保证三点（每篇约 1 分钟）：

1. **有基本标题层级**（Markdown `#`~`####`）——标题是结构切分的主信号；
2. **文本类优先转 md/txt**——本地解析免费、切分最准；扫描件/复杂排版自动走 MinerU；
3. **编码保持 UTF-8**（GBK 有兜底，但 UTF-8 最稳）。

程序自动处理：格式/大小校验 → 编码回退 → 复杂度评估分流 → 结构感知切分（标题分节、表格/公式/**问答对**原子保护）→ 增量同步。

决策规则：单篇整理 < 5 分钟就人工整理；同一种脏格式出现 ≥ 3 次才写代码适配（如问答对原子保护）；偶发怪文档清洗一次或不入库。

## 核心设计

### 指纹口径（三把钥匙）

| 指纹 | 计算 | 用途 |
| --- | --- | --- |
| `identity_hash` | `SHA256(file_name \| source)` | 同名同源文件身份判定（唯一） |
| `file_content_hash` | `SHA256(文件字节)` | 文件内容是否变化 |
| `chunk_fingerprint` | `SHA256(source \| content)` | chunk 去重，同时作为 **Milvus 主键** |

> 指纹全部以 `source` 参与哈希，保证 MySQL 指纹与 Milvus 主键口径一致，是增量更新与幂等写入的基础。

### 增量入库流程

```
校验 → 预检(加载前, 只算哈希查库) → 判定动作:
  skip   : 内容未变且已同步 → 跳过加载
  update : 内容已变 → 加载切分 → 差集更新(新增/未变只刷版本/删除)
  insert : 全新文件 → 落库 v1.0
  retry  : 上次同步失败 → 按 source 清空该文件向量后全量重放
```

### 跨库一致性（两阶段 + 状态机）

1. **阶段一（MySQL）**：把期望状态落库并提交（`sync_status=pending`）。失败即回滚，Milvus 完全未动，不产生孤儿向量。
2. **阶段二（Milvus）**：按确定性 ID 幂等同步（先删后插）。成功置 `in_sync`；失败置 `failed + last_error`，重跑自动恢复。

### 入库任务队列（Redis Streams）

```
上传 → XADD ingest_queue → worker XREADGROUP 消费 → ingest_files()
     ├─ 成功 → XACK + XDEL（消息不滞留）
     ├─ 失败 → 指数退避重入队（2s/4s），超 3 次进死信队列
     └─ 崩溃 → PEL 残留任务由 XAUTOCLAIM 重启回收
```

- `ingest:inflight` 集合标记"正在入库"，上传/删除接口返回 **409**，防"上传后立刻删除"的孤儿向量竞态；
- Redis 不可用 → 上传、删除、账户删除等写操作返回 **503**，避免进程内任务因崩溃丢失。

### 会话记忆（Postgres + TTL + 摘要）

- 记忆按 `thread_id = {user_id}:{session_id}` 隔离，Postgres 持久化（跨重启/多 worker）；
- **TTL**：以线程"最后活跃时间"为准，超过 `MEMORY_TTL_DAYS`（默认 1 天）整线程清理（后台任务周期执行）；
- **摘要**：约 20 轮对话（40 条消息）或 6000 tokens 触发 SummarizationMiddleware 压缩，保留最近 10 条，上下文不无限增长。

## 评测与测试

```bash
# 检索评测（golden 集 25 题 → hit@k + 精排分）
python -m evaluation.run_evaluation

# 分层检索对比实验（普通块 vs 子→父）
python -m evaluation.experiment_parent_child

# 单元测试
python -m pytest tests/
```

当前基线：**hit@1 = 80% / hit@3 = 100% / hit@5 = 100%**，平均精排分 0.89（`evaluation/report.json`）。

## API 一览

| 方法 | 路径 | 权限 | 说明 |
| --- | --- | --- | --- |
| POST | `/api/auth/register` | 公开 | 注册（默认普通用户） |
| POST | `/api/auth/login` | 公开 | 登录，返回 JWT |
| GET | `/api/auth/me` | 登录 | 当前用户信息 |
| POST | `/api/documents/upload` | 登录 | 上传自己的文档（异步队列入库，默认私有，可指定 `is_public`） |
| GET | `/api/documents` | 登录 | 文档列表（仅自己 + 共享，source 脱敏） |
| DELETE | `/api/documents/{id}` | 文档 owner | 删除自己的文档（正在入库返回 409） |
| POST | `/api/documents/{id}/revoke` | 管理员 | 把共享文档取消为私有 |
| GET | `/api/documents/{id}/download` | owner 或共享 | 下载文档原件（私有他人文档返回 404） |
| POST | `/api/auth/delete-account` | 登录 | 提交账户删除（状态 deleting，进入删除队列） |
| POST | `/api/chat` | 登录 | 知识库问答（多问题拆分 + 多轮记忆） |
| POST | `/api/chat/stream` | 登录 | **SSE 流式问答**（meta → token×N → done） |
| POST | `/api/search` | 登录 | 语义检索 |
| GET | `/api/ingest/stats` | 登录 | 入库队列状态 |
| GET | `/chat` | 公开 | 浏览器问答页面 |
| GET | `/docs` | 公开 | Swagger |

## 项目结构

```
rag_project/
├─ rag个人知识库/
│  ├─ api/                  # FastAPI：main(路由) / auth(JWT+RBAC+限流) / static(问答页)
│  ├─ agent/                # ai_assist(Agent+记忆) / intent(意图+查询重构) / model(DeepSeek)
│  ├─ service/              # ingest(入库编排) / chat(问答编排) / document_admin(删除)
│  │                        # ingest_queue(入库队列) / delete_queue(账户删除队列) / memory_maintenance(TTL清理)
│  ├─ loader/               # 文档加载（docx/pdf/md/txt，复杂度评估 + MinerU 分流）
│  ├─ spliter/              # 结构感知切分（标题分节 + 原子块保护）
│  ├─ vector_store/         # Milvus 存取 + 双路召回 + rerank
│  ├─ crud/                 # MySQL 数据访问
│  ├─ models/               # SQLAlchemy 模型 + 建表 SQL（vector.sql / postgres_memory.sql）
│  ├─ config/               # db_config(MySQL) / redis(Redis)
│  └─ utils/                # 指纹哈希
├─ evaluation/              # golden 评测集 + 评测/实验脚本 + 报告
├─ tests/                   # pytest 单元测试（52 用例）
└─ .env.example             # 环境变量模板
```

## 路线图

- [x] 增量同步 + 双库一致性状态机
- [x] FastAPI 后端（JWT + RBAC + 文档管理 + 问答/检索接口）
- [x] Agent 问答 + 多轮记忆（Postgres 持久化 + TTL + 摘要）
- [x] Redis Streams 入库任务队列 + 竞态防护
- [x] golden 评测集 + 分层检索对比实验 + pytest 套件
- [x] docker-compose 一键编排（MySQL/Milvus/Redis/Postgres/API）
- [x] SSE 流式输出 + Vue 3 + TypeScript 前端
- [x] 文档归属/共享/取消共享/下载权限
- [x] 账户删除队列（Milvus → OSS → 本地 → MySQL → 缓存清理）
- [x] 多问题问答编排
- [x] 精准缓存失效（src_idx 索引）
- [ ] 分层检索（Parent-Child）落地（实验结论已具备）
- [ ] Agentic RAG（检索工具化，多轮反思检索）
