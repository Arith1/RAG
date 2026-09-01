# RAG 个人知识库

基于**文件指纹增量同步 + MySQL/Milvus 双库协作**的 RAG 问答系统：文档入库自动判定同名同源与内容变化（未变跳过 / 变更增量更新 / 失败可重试），检索侧双路召回 + 重排精排，接入 Agent 生成带来源引用的回答；配套完整 Web 服务（JWT + RBAC）、会话记忆（Postgres 持久化）、可靠入库任务队列（Redis Streams）。

> 核心亮点：**四存储各司其职**——MySQL（元数据权威）、Milvus（向量检索）、Postgres（对话记忆）、Redis（任务队列/限流/缓存索引）；两阶段落库 + 状态机保障跨库一致性，杜绝孤儿向量；上传/删除/账户删除等写操作依赖 Redis Streams 保证任务不丢，检索问答在依赖不可用时尽量降级。

---

## 功能特性

- **文件指纹增量入库**：`identity_hash` 判定同名同源，`file_content_hash` 判定内容变化——skip / insert / update / retry 四态分流，只增量更新差集。
- **跨库一致性**：MySQL 先落期望状态（`pending`）→ Milvus 幂等同步 → `in_sync`/`failed` 状态机；失败重试按 source 重建，可清理孤儿向量。
- **结构感知切分**：Markdown 标题分节、**表格/公式/问答对原子保护**（占位符 + 还原长度分组）、超长表格自动拆子表并重复表头、图片路径入 metadata。
- **高质量检索**：dense（bge-m3）+ BM25（jieba 中文分词）双路召回 → RRF 融合 → bge-reranker-v2-m3 精排 → 阈值过滤；支持 `source` / 原生表达式过滤（二者互斥，可叠加 `file_ids` 可见性过滤，并可独立调 `fetch_k` 放大召回宽度）；**检索结果 / embedding / 回答三层 Redis 缓存**（相同问题秒回、省 API 调用）。
- **Agent 问答**：意图识别（规则层 + LLM 查询重构，多轮指代补全）→ 检索 → DeepSeek Agent 生成带来源引用回答；对话记忆按用户隔离。
- **会话记忆**：Postgres 持久化（跨重启/多 worker），TTL 按"最后活跃时间"自动清理，~20 轮对话自动摘要压缩。
- **可靠入库队列**：Redis Streams（Consumer Group + PEL 崩溃恢复、持久化延迟重试、死信队列、inflight 竞态防护 409）。
- **可靠删除队列**：账户删除同样走 Redis Streams，按 Milvus → OSS → 本地文件 → MySQL → 缓存清理顺序执行，失败持久化重试，不卡账号。
- **权限模型**：普通用户可上传/删除自己的文档；管理员可把共享文档取消为私有；下载仅 owner 或共享文档可访问。
- **多问题问答**：意图识别支持拆分多个子问题，逐个检索后汇总分点回答，并限制最大子问题数。
- **精准缓存失效**：维护 `src_idx:{source}` 缓存索引，文档取消共享/删除/账户删除/重新入库时按 source O(1) 定位清理相关检索与回答缓存（入库改为按 source 精准失效，不再全库清空）。
- **Milvus 连接自愈**：向量库实例连接/执行异常时自动重建单例，检索与写入不会因一次断线而永久失败；写操作不盲目自动重试，避免重复插入。
- **多人共享知识库**：文档可设为私有/公开，普通用户与管理员都能管理自己的文档；管理员可把他人公开文档设为私有（审核）；知识库页支持搜索、按最近更新/最近上传/下载量/chunk 数排序与下载。
- **会话检索范围**：新建会话时可选 4 个检索范围（自己的私有 / 自己的公开 / 知识库公开 / 指定用户公开），首问后锁定不可更改。
- **历史会话 + Redis 缓存**：MySQL 存会话列表/摘要、Postgres 存完整消息；登录后后台预热「会话列表 + 最近 10 个会话记录」到 Redis（Cache-Aside + 1h TTL），会话变更精准失效。
- **高频访问缓存**：鉴权用户行（登出/改密/删号失效）、文档列表、用户搜索均缓存到 Redis，显著减少高频接口 DB 查询。
- **前端定时轮询**：文档管理页每 5s 自动刷新文档列表与入库队列状态（入库中/失败实时可见）。
- **Web 服务**：FastAPI + JWT 认证 + RBAC + 操作审计 + 登录/注册失败限流 + 路径脱敏。
- **可度量**：25 题 golden 评测集（hit@3 = 100%）、分层检索对比实验、DeepEval 端到端评测（Faithfulness / AnswerRelevancy / 上下文精度召回）、52 个 pytest 用例。

## 系统架构

```mermaid
flowchart LR
    subgraph Web["FastAPI 服务"]
        A["Web 前端(5173)<br/>/api/*"]
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
# 1. 配置密钥（复制模板后填写 SILICONFLOW_API_KEY / MINERU_API_TOKEN / DEEPSEEK_API_KEY /
#    JWT_SECRET / ADMIN_PASSWORD，以及 MYSQL_PASSWORD / POSTGRES_PASSWORD / REDIS_PASSWORD）
cp .env.example .env

# 2. 一键启动全部服务（MySQL / Milvus(含 etcd+minio) / Redis / Postgres / API）
#    安全说明：数据服务端口仅绑定 127.0.0.1；Redis/MySQL/Postgres 口令取自 .env（缺失拒绝启动）；
#    Milvus 已开启认证（docker/milvus.yaml，root/Milvus，启动后建议立即改密）
docker compose up -d --build

# 3. 访问
#    前端(开发) http://localhost:5173     Swagger: http://localhost:8010/docs
```

> **生产部署建议**：API 8010 前置 Nginx/Caddy 反代 + HTTPS；如不需要宿主机直连数据库，
> 可去掉 `docker-compose.yml` 里 MySQL/Redis/Postgres/Milvus 的端口映射（仅容器内网互通）。

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

# 2. Postgres 对话记忆：langgraph 首次使用时自动创建 checkpoint 表，无需手动建表
```

> 表结构采用**手动 SQL 管理**（`models/*.sql`），代码不做任何 DDL，改表后需同步 SQL 文件并手动执行。

### 启动服务

```bash
uvicorn rag个人知识库.api.main:app --host 0.0.0.0 --port 8010
```

- **前端页面**：开发 http://localhost:5173（`cd rag_frontend && npm run dev`）；生产由 Nginx 托管构建产物
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

前端功能：登录/注册（JWT）、**SSE 流式问答**（打字机效果 + 来源引用 + 历史会话侧边栏 + 检索范围选择）、知识库（公开文档搜索/排序/下载）、文档管理（私有/共享分区 + 上传队列 5s 轮询）、个人详情页。

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

> **隐私说明**：扫描件、复杂排版的 Word/PDF 会自动调用**第三方 MinerU 云解析服务**（https://mineru.net）处理，文档原文会发送到该服务解析后再回传结果；涉及敏感/机密内容请优先转换为 md/txt（本地解析，内容不离开服务器），或评估后再上传。

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

### 检索过滤、召回与容错

- **过滤条件**：`source`（按来源，便捷）与 `expr`（原生 Milvus 表达式）**互斥**，同时传入直接抛 `ValueError` 避免歧义；二者均可与 `file_ids`（可见性文件 id 集合）叠加，`file_ids` 作为最外层用 `and` 组合。
- **召回宽度**：`search()` 支持独立 `fetch_k` 参数（默认等于 `k`），放大后让 RRF 在更宽的候选中融合再交给 reranker 精排；精排入口 `_search_with_rerank_metrics` 默认以 `recall_k=20` 召回（可用 `RAG_RECALL_K` 调整）。
- **精排保底填充（fill-to-k）**：rerank 后先取达标（≥ `RERANK_SCORE_THRESHOLD`）候选；若不足 `k`，用未达标中分数最高者补齐到 `k`（标记 `rerank_low_score`），避免小知识库 + 阈值一刀切把真正的答案 chunk 误杀导致返回数远小于 `k`（实测由 2~5 条补满）。
- **断线自愈**：所有 Milvus 操作统一经 `_milvus_op` 包装，异常时自动丢弃单例缓存、下次调用重建连接；写操作不自动重试，避免重复写入。

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
- **TTL**：以 `chat_sessions.updated_at`（会话最后活跃时间）为准，超过 `MEMORY_TTL_DAYS`（默认 1 天）整线程清理（后台任务周期执行）；
- **摘要**：约 20 轮对话（40 条消息）或 6000 tokens 触发 SummarizationMiddleware 压缩，保留最近 10 条，上下文不无限增长。

### 会话与高频访问缓存（Redis Cache-Aside）

| Key | 内容 | TTL | 失效时机 |
| --- | --- | --- | --- |
| `sess:list:{user_id}` | 会话列表（问答侧边栏） | 1h | 问答结束 / 重命名 / 删除会话 |
| `sess:detail:{user_id}:{session_id}` | 单会话完整记录（含消息） | 1h | 该会话有新问答 / 被删除 |
| `sess:detail_idx:{user_id}` | 已缓存详情的 session_id 集合 | 1h | 删除账号时整批清理 |
| `usr:{user_id}` | 鉴权用户行（`get_current_user`） | 1h | 登出 / 改密 / 删除账号 |
| `docs:{user_id}:{limit}:{offset}` | 文档列表分页 | 60s | 上传 / 删除 / 共享 / 入库完成 |
| `users:{viewer}:{limit}:{q}` | 用户搜索（指定用户多选器） | 5min | 注册 / 删除账号 |

- 登录成功后**后台预热**「会话列表 + 最近 10 个会话记录」，不阻塞登录响应；
- 全部走 Cache-Aside：命中直接返回，未命中回源 DB/Postgres 后写回；Redis 不可用时自动降级，行为与无缓存一致。

### RAG 全链路可观测

**每次请求（chat 或 search）写一条 `rag_traces`**，覆盖「入口 → 意图 → 检索 → 精排 → 生成 → 落库」每一跳：

- **入口**：chat 与 `/api/search` 都写 trace（`trace_type=chat/search`）；记录 `query_raw`（原始输入）与 `query`（提炼后），可定位「相同问题缓存 miss」是否为意图提炼漂移。
- **检索分跳计时**：`embedding_ms` / `milvus_ms` / `rerank_ms` / `cache_ms` 分开统计（`retrieval_ms` 为整段，含前三者）。
- **监控页（ObsView）**：时间范围聚合（请求量 / 成功率 / 各阶段均耗时 / 零命中率 / 降级率 / **缓存命中率**，聚合 chat+search）、**Top 慢请求**、**失败分布**、意图分布、单条 trace **瀑布图**。
- **缓存命中计费**：回答命中 `ans:` 缓存时，用量记录一条 `type=answer_cached`（tokens=0 / cost=0，`status=cached`），「最近用量」可看到省下的生成费用；检索缓存命中率以 trace 聚合为准，进程内计数标注「本进程实时」。
- **写记忆异步**：`append_thread_exchange`（Postgres 对话记忆）改为后台异步写入，不阻塞回答返回，避免未计时的写库拖慢总耗时。

## 评测与测试

```bash
# 检索评测（golden 集 25 题 → hit@k + 精排分）
python -m evaluation.run_evaluation

# DeepEval 端到端评测（检索 + Agent 生成 + LLM 评判）
#   依赖 deepeval（uv sync --dev 已含）+ 可联网访问 embedding/Agent/judge
#   Judge 默认复用 DashScope 配置（也可用 JUDGE_BASE_URL/JUDGE_API_KEY/JUDGE_MODEL 覆盖）
python -m evaluation.evaluate_rag_deepeval                      # 端到端：Faithfulness / AnswerRelevancy 等
python -m evaluation.evaluate_rag_deepeval --mode retrieval     # 仅检索：top-1 片段代理，只跑 AnswerRelevancy
python -m evaluation.evaluate_rag_deepeval --limit 3            # 冒烟：只跑前 3 题
# 可调项：--concurrency（judge 并发，默认 2，防限流）/ --throttle（请求间隔）/ --threshold
# 网络慢导致 judge 超时时：DEEPEVAL_JUDGE_TIMEOUT=120 python -m ...

# 分层检索对比实验（普通块 vs 子→父）
python -m evaluation.experiment_parent_child

# 单元测试
python -m pytest tests/
```

- 检索基线：**hit@1 = 80% / hit@3 = 100% / hit@5 = 100%**，平均精排分 0.89（`evaluation/report.json`）。
- DeepEval 报告：`evaluation/deepeval_report.json`（生成侧质量，与检索 hit@k 互补）。
- 说明：已适配 deepeval 4.x（judge 经 metric 的 `model=` 传入 OpenAIModel 指向 OpenAI 兼容端点）；仅检索模式下 `Faithfulness` 因 top-1 即上下文而恒为 1.0（代理值），`AnswerRelevancy` 是有效信号；golden 每题补 `expected_answer`（旁挂 `evaluation/expected_answers.json`）后还会启用 ContextualPrecision / ContextualRecall。

#### DeepEval 端到端评测基线（24 题，排除未入库的 langchain-1）

> judge = `deepseek-v4-flash` · mode = end-to-end · k=5 / recall_k=20 / threshold=0.7 · 结果见 `evaluation/deepeval_report_no_langchain1.json`

| 指标 | 数值 |
| --- | --- |
| hit@1 / hit@k | 79.2%（19/24）/ 100% |
| **Faithfulness** | **0.99** |
| **AnswerRelevancy** | **0.93** |
| **ContextualPrecision** | **0.97** |
| **ContextualRecall** | **0.93** |

- **结论**：端到端生成质量良好（AR 0.93 / Faith 0.99）。此前 retrieval 模式 AR=0.65 是「Top-1 片段必须直接回答」的严格代理，不代表真实生成质量。
- **未通过（任一指标 < 0.7，jwt-3 已修复剔除）**：
  - `session-2`（AR 0.56）—— 生成跑题（答里掺杂 TTL/summarizer 等无关内容）
  - `queue-4`（CR 0.50）/ `session-3`（CR 0.50）/ `loader-2`（CR 0.67）—— 答案跨多个小节、Top-5 未完全覆盖（真实覆盖缺口）
  - ~~`jwt-3`~~ —— 原 CR 0.67 为 draft expected_answer 假低分；修正后单跑 CR = **1.0**（详见 `evaluation/deepeval_report.md`）

#### 提高 DeepEval 通过率的方法

1. **先修 `expected_answer` 口径**（成本最低）：CR 是「每句都必须在检索上下文里被支持」的逐句指标。若 draft 含源文档里没有的推断句（如 jwt-3 的「攻击者无法换算法」）会假低分；把 draft 收紧到源文档可验证的表述，重跑即可区分「真缺口 vs 假低分」。
2. **提升检索覆盖（治 CR 低分）**：queue-4 / session-3 / loader-2 的答案分散在多个小节、Top-5 抓不全 → 各节开头补「结论一句话」、或小 chunk + Parent-Child 分层检索、或按需放大 `recall_k`。
3. **生成聚焦（治 AR 低分）**：session-2 回答跑题 → 收紧生成 prompt，让回答直接命中问题、避免堆砌无关模块细节。
4. **评测侧**：judge 多轮取均值（消 LLM 方差）、阈值用 0.65~0.7、报告里保存 `actual_output` 便于定位生成问题；补 `expected_answer` 后 CR/CP 才有意义。

## API 一览

| 方法 | 路径 | 权限 | 说明 |
| --- | --- | --- | --- |
| POST | `/api/auth/register` | 公开 | 注册（默认普通用户） |
| POST | `/api/auth/login` | 公开 | 登录，返回 JWT |
| GET | `/api/auth/me` | 登录 | 当前用户信息 |
| GET | `/api/users/search` | 登录 | 用户搜索（指定用户检索范围选择器） |
| GET | `/api/users/{id}/profile` | 登录 | 用户个人详情（公开字段，他人可访问） |
| POST | `/api/documents/upload` | 登录 | 上传自己的文档（异步队列入库，默认私有，可指定 `is_public`） |
| GET | `/api/documents` | 登录 | 文档列表（仅自己 + 共享，source 脱敏） |
| DELETE | `/api/documents/{id}` | 文档 owner | 删除自己的文档（正在入库返回 409） |
| POST | `/api/documents/{id}/revoke` | owner/管理员 | 把共享文档取消为私有（管理员可审核他人） |
| POST | `/api/documents/{id}/share` | owner/管理员 | 把文档设为公开共享 |
| GET | `/api/documents/{id}/download` | owner 或共享 | 下载文档原件（私有他人文档返回 404） |
| POST | `/api/auth/delete-account` | 登录 | 提交账户删除（状态 deleting，进入删除队列） |
| POST | `/api/auth/logout` | 登录 | 登出并清除该用户的鉴权缓存 |
| POST | `/api/chat` | 登录 | 知识库问答（多问题拆分 + 多轮记忆） |
| POST | `/api/chat/stream` | 登录 | **SSE 流式问答**（meta → token×N → done） |
| GET | `/api/chat/sessions` | 登录 | 历史会话列表（Redis 缓存） |
| GET | `/api/chat/sessions/{id}` | 登录 | 单会话完整记录（Postgres 消息 + Redis 缓存） |
| PATCH | `/api/chat/sessions/{id}` | 登录 | 重命名会话 |
| DELETE | `/api/chat/sessions/{id}` | 登录 | 删除会话（同步清 Postgres 记忆） |
| POST | `/api/search` | 登录 | 语义检索（双路召回 + 精排，支持 `source` 过滤） |
| GET | `/api/ingest/stats` | 登录 | 入库队列状态 |
| GET | `/api/ingest/queue` | 登录 | 入库队列：待处理任务 |
| GET | `/api/ingest/inflight` | 登录 | 入库队列：正在入库 |
| GET | `/api/ingest/dead` | 登录 | 入库队列：死信（失败）任务 |
| POST | `/api/ingest/dead/{msg_id}/retry` | 管理员 | 死信单条重新入库 |
| POST | `/api/ingest/dead/retry-all` | 管理员 | 死信全部重新入库 |
| DELETE | `/api/ingest/dead` | 管理员 | 清空死信队列 |
| GET | `/docs` | 公开 | Swagger |

## 项目结构

```
rag_project/
├─ rag个人知识库/
│  ├─ api/                  # FastAPI：main(路由) / auth(JWT+RBAC+限流) / static(问答页)
│  ├─ agent/                # ai_assist(Agent+记忆) / intent(意图+查询重构) / model(DeepSeek)
│  ├─ service/              # ingest(入库编排) / chat(问答编排) / chat_history(会话元信息)
│  │                        # session_cache(会话/用户/文档缓存) / ingest_queue(入库队列)
│  │                        # delete_queue(账户删除队列) / memory_maintenance(TTL清理)
│  ├─ loader/               # 文档加载（docx/pdf/md/txt，复杂度评估 + MinerU 分流）
│  ├─ spliter/              # 结构感知切分（标题分节 + 原子块保护）
│  ├─ vector_store/         # Milvus 存取 + 双路召回 + rerank（断线自愈）
│  ├─ crud/                 # MySQL 数据访问
│  ├─ models/               # SQLAlchemy 模型 + 建表 SQL（vector.sql）
│  ├─ config/               # db_config(MySQL) / redis(Redis)
│  └─ utils/                # 指纹哈希 / sanitize(路径脱敏)
├─ rag_frontend/            # Vue 3 前端（问答/知识库/文档管理/个人详情/登录注册）
├─ evaluation/              # golden 评测集 + 检索评测 + DeepEval 端到端评测 + 报告
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
- [x] 多人共享知识库 + 会话检索范围（4 选，首问锁定）
- [x] 历史会话侧边栏 + Redis 会话/用户/文档缓存 + 登出清理
- [x] 前端定时轮询队列状态（5s）
- [ ] 分层检索（Parent-Child）落地（实验结论已具备）
- [ ] Agentic RAG（检索工具化，多轮反思检索）
