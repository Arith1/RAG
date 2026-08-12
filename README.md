# rag-project · 文件指纹增量入库的 RAG 知识库

基于**文件指纹 + MySQL/Milvus 双库协作**的 RAG 知识库系统：文件入库时自动判定同名同源、内容是否变化，做到「未变跳过、变更更新、失败重试」的增量同步；检索侧双路召回 + 重排精排，并支持按文档过滤。

> 核心亮点：MySQL 元数据与 Milvus 向量通过**两阶段落库 + 同步状态机**保障跨库一致性，杜绝孤儿向量与丢失向量。

---

## 功能特性

- **文件指纹增量入库**：`file_name + source` 判定同名同源，`内容 SHA256` 判定是否变化——未变直接跳过加载、变更只更新差集、全新入库、失败可重跑。
- **跨库一致性保障**：MySQL 先落「期望状态」（`pending`）→ Milvus 幂等同步 → `in_sync` / `failed` 状态机；失败重试按 source 单文件重建，可清理中途失败残留的孤儿向量。
- **结构感知切分**：Markdown 标题分节、公式/表格原子块保护、超长表格自动拆分子表并重复表头。
- **高质量检索**：dense（bge-m3）+ BM25 双路召回 → RRF 融合 → bge-reranker 精排 → 阈值过滤；支持 `source` / 原生表达式过滤。
- **异步化**：Milvus 写入、删除、检索均封装为异步版本（线程池执行），便于后续接入 FastAPI。
- **业务层与 CLI 解耦**：`service` 层返回 JSON 友好结构，CLI 只是薄壳，前端可直接复用。

## 架构

```mermaid
flowchart LR
    subgraph 入库管线
        A[文档] --> B[预检·计算指纹]
        B --> C{内容变化?}
        C -- 未变且已同步 --> D[跳过]
        C -- 变更 / 全新 --> E[加载 + 结构切分]
        E --> F[(MySQL 落期望状态 pending)]
        F --> G[(Milvus 幂等同步)]
        G --> H[置为 in_sync]
        G -.失败.-> I[failed + last_error]
        I --> B
    end

    subgraph 检索管线
        Q[问题] --> R[双路召回<br/>dense + BM25]
        R --> S[RRF 融合]
        S --> T[bge-reranker 精排]
        T --> U[阈值过滤]
        U --> V[结果 + 来源]
    end
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
| 元数据存储 | MySQL 8.x + SQLAlchemy 2.0（async）+ aiomysql |
| 向量存储 | Milvus 2.x + pymilvus + langchain-milvus |
| Embedding / Rerank | SiliconFlow：`BAAI/bge-m3`、`BAAI/bge-reranker-v2-m3` |
| 文档解析 | MinerU（复杂文档）+ Unstructured（简单文档） |
| 框架 | langchain 生态（core / community / text-splitters） |

## 快速开始

### 环境要求

- Python 3.14+
- MySQL 8.x（本地或远程均可）
- Milvus（Docker 一键启动）

```bash
# 启动 Milvus standalone
docker run -d --name milvus \
  -p 19530:19530 -p 9091:9091 \
  milvusdb/milvus:v2.5.4 standalone
```

### 安装

```bash
git clone https://github.com/Arith1/RAG-.git
cd rag_project

# 推荐使用 uv
uv sync

# 复制环境变量模板并填写密钥
cp .env.example .env
# Windows: copy .env.example .env
```

`.env` 必需配置项：

| 变量 | 说明 |
| --- | --- |
| `ASYNC_DATABASE_URL` | MySQL 连接串，如 `mysql+aiomysql://root:root@localhost:3306/rag_demo?charset=utf8` |
| `SILICONFLOW_API_KEY` | SiliconFlow 密钥（embedding + rerank 用） |
| `MINERU_API_TOKEN` | MinerU 解析服务 Token（复杂 Word/PDF 用） |
| `MILVUS_URI` | Milvus 地址，默认 `http://localhost:19530` |

### 初始化数据库

```bash
mysql -uroot -p -e "CREATE DATABASE rag_demo CHARACTER SET utf8mb4;"
mysql -uroot -p rag_demo < rag个人知识库/models/vector.sql
```

> 表结构采用**手动 SQL 管理**（`models/vector.sql`），代码不做自动迁移，避免隐式改表。

### 入库文档

```bash
# 把待入库文档放入 rag个人知识库/resources/（该目录已 gitignore），
# 或直接修改 main.py 中的 file_path_list
python -m rag个人知识库.main ingest
```

输出示例：`全新入库 v1.0` / `内容变更，已更新（新增 3 / 未变 20 / 删除 1）` / `内容未变，已跳过`。

### 检索

```bash
# 列出已入库文档
python -m rag个人知识库.main search --list

# 全库检索
python -m rag个人知识库.main search "LangChain 是什么" -k 3

# 指定文档内检索
python -m rag个人知识库.main search "LangChain 是什么" --source "F:\path\AI智能体开发框架LangChain.docx"
```

## 项目结构

```
rag_project/
├─ rag个人知识库/
│  ├─ main.py                # CLI 入口（ingest / search）
│  ├─ service/
│  │  ├─ ingest.py           # 入库编排：两阶段落库 + Milvus 同步
│  │  └─ service.py          # 业务层：ingest_files / search_documents / list_documents
│  ├─ loader/
│  │  ├─ load_file.py        # 文档加载（docx/pdf/md/txt，UTF-8/GBK 回退）
│  │  └─ parser/             # MinerU / Word 复杂度解析
│  ├─ spliter/               # 结构感知切分
│  ├─ vector_store/          # Milvus 存取 + 双路召回 + rerank 精排
│  ├─ crud/                  # MySQL 数据访问
│  ├─ models/                # SQLAlchemy 模型 + 建表 SQL（models/vector.sql）
│  ├─ config/                # 数据库引擎与会话
│  └─ utils/                 # 指纹哈希
└─ .env.example              # 环境变量模板
```

## 核心设计

### 指纹口径（三把钥匙）

| 指纹 | 计算 | 用途 |
| --- | --- | --- |
| `identity_hash` | `SHA256(file_name \| source)` | 同名同源文件身份判定（唯一） |
| `file_content_hash` | `SHA256(文件字节)` | 文件内容是否变化 |
| `chunk_fingerprint` | `SHA256(source \| content)` | chunk 去重，同时作为 **Milvus 主键** |

> 指纹全部以 `source` 参与哈希，保证 MySQL 指纹与 Milvus 主键口径完全一致，是增量更新与幂等写入的基础。

### 增量入库流程

```
校验 → 预检(加载前, 只算哈希查库) → 判定动作:
  skip   : 内容未变且已同步 → 跳过加载
  update : 内容已变 → 加载切分 → 差集更新(新增/未变只刷版本/删除)
  insert : 全新文件 → 落库 v1.0
  retry  : 上次同步失败 → 按 source 清空该文件向量后全量重放
```

### 跨库一致性（两阶段 + 状态机）

1. **阶段一（MySQL）**：把期望状态落库并提交（`sync_status=pending`）。失败即回滚，此时 Milvus 完全未动，不会产生孤儿向量。
2. **阶段二（Milvus）**：按确定性 ID 幂等同步（先删后插）。成功置 `in_sync`；失败置 `failed + last_error`，期望状态保留在 MySQL，重跑自动恢复。

## 路线图

- [ ] FastAPI 后端 + Web 前端（文档管理 / 对话界面）
- [ ] LLM 问答：RAG 检索结果 + 大模型生成，带来源引用
- [ ] 核心逻辑单元测试（指纹、差集、状态机）
- [ ] 文件存储抽象层（本地实现，可插拔切换阿里 OSS）