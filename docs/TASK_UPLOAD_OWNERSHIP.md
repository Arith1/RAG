# 任务清单：上传归属 + 检索可见性（第一阶段）

> 目标：文档按用户归属，支持私有/共享；普通用户可上传自己的文档；检索只返回当前用户可见的文档。
> 状态跟踪文件，供后续阶段（is_public 开关、权限细化、账户删除、guest 角色）复用。

---

## 一、权限模型（已确认）

| 角色 | 上传 | 修改/删除 | 检索 | 额外 |
| --- | --- | --- | --- | --- |
| user 普通用户 | ✅ 自己的（默认私有，可勾选公开） | ✅ 自己的 | 共享(is_public=1) + 自己的 | — |
| admin 管理员 | ✅ 自己的 | ✅ 自己的 | 共享 + 自己的 | 把他人共享文档取消为私有（后续阶段） |
| guest 访客 | ❌ 暂不开放注册 | — | 默认同普通用户（预留） | — |

> 所有登录用户可检索 `is_public=1` 文档；私有文档仅 owner 可见；**admin 检索范围同普通用户**（只多"取消共享"权限）。

---

## 二、已完成：SQL 表结构

- [x] `rag个人知识库/models/vector.sql`
  - `vector_files` 新增 `owner_id BIGINT UNSIGNED NOT NULL`（FK→users.id，**ON DELETE CASCADE**）+ `is_public TINYINT(1) NOT NULL DEFAULT 0`
  - 索引 `idx_owner_id` / `idx_is_public`
  - `users.role` 注释补 `guest`
- [x] `rag个人知识库/models/migration_owner_public.sql`（增量迁移脚本，正确顺序：加列→回填→转非空→建索引→建外键）
- [x] `rag个人知识库/models/migration_user_status.sql`（users.status：active/deleting/disabled，默认 active）
- [x] 提示：`owner_id 外键 CASCADE` + `chunk_records.file_id CASCADE` → 用户删除时 SQL 层层级联

## 三、已完成：ORM 同步

- [x] `models/user.py`：`status` 字段（默认 active）+ `documents` relationship（back_populates="owner"）
- [x] `models/vector.py`：`owner_id`（非空 FK CASCADE）+ `is_public`（Boolean 默认 0）+ 索引 + `owner` relationship（TYPE_CHECKING 避免循环导入）

## 四、已完成：上传归属链路

- [x] `crud/vector.py`：`insert_file` 增加 `owner_id` / `is_public` 参数
- [x] `service/ingest.py`：
  - `_stage_insert` 透传 owner_id/is_public → insert_file
  - `process_file` 增加 owner_id/is_public 参数；阶段一后把 **`file_id` + `owner_id` 写入 chunk metadata**（Milvus 过滤载体）
  - `ingest_files_batched` 透传 owner_id/is_public
- [x] `service/ingest_queue.py`：`enqueue_ingest` 消息带 `owner_id`/`is_public`；`process_message` 解析并传给 `ingest_files`
- [x] `service/service.py`：`ingest_files` 增加 owner_id/is_public 参数

## 五、已完成：检索可见性（核心安全）

- [x] `crud/vector.py`：新增 `select_visible_file_ids(db, user_id)`（本人 OR is_public=1）；`select_file_names` 支持 user_id 过滤
- [x] `vector_store/milvus_store.py`：`search` / `search_with_rerank` / `asearch_with_rerank` 增加 `file_ids` 参数 → `_file_ids_expr`（`file_id in [...]`）
- [x] `service/service.py`：
  - `search_documents` 增加 `user_id`：先查可见 file_ids → 空则直接返回空 → 传 file_ids 过滤 Milvus
  - **缓存 key 纳入 user_id**（`cache_key("search", query, k, source, expr, user_id)`，防跨用户缓存串号）
  - `list_documents` 增加 user_id 过滤（返回列表含 owner_id/is_public）
- [x] `service/chat.py`：`chat` / `chat_stream` 增加 user_id 透传检索

## 六、已完成：API 层

- [x] `api/main.py`：
  - `upload_document`：鉴权 `require_admin` → **`get_current_user`**（普通用户可上传）；新增 `is_public` 表单参数（默认私有）；传 owner_id/is_public 给入队/后台任务
  - `list_docs`：传 `user_id`（只列自己 + 共享）
  - `search_api` / `chat_api` / `chat_stream_api`：传 `user_id`（检索仅返回可见文档）
  - import 增加 `Form`

## 七、已完成：修复与验证

- [x] `.env` 修复：`EMBEDDING_CACHE_TTL=604,800` → `604800`（千分位逗号导致 int() 崩溃，阻断所有依赖 milvus_store 的模块）
- [x] **AST 语法检查**：7 个改动文件全部通过
- [x] **import 冒烟**：全部模块可加载，无循环 import；`_file_ids_expr([1,2,3]) = "file_id in [1,2,3]"`
- [x] **pytest**：43 passed, 3 skipped（无失败，未破坏现有测试）

## 八、已完成：端到端验证

- [x] 注册普通用户 → 上传私有/共享文档 → 验证 `owner_id`/`is_public` 落库
- [x] 验证 chunk metadata 含 `file_id`/`owner_id`
- [x] 验证检索可见性：A 用户私有文档 B 用户检索不到；共享文档所有用户可见
- [x] 验证缓存 key 带 user_id（同 query 不同用户不串数据）

**验证结果**（2026-08-22 实跑，API + MySQL + Redis + Milvus 均可用）：

- 使用普通用户 A/B 注册登录后，A 上传私有与共享 txt 各 1 篇
- MySQL `vector_files` 中 `owner_id=A.id`、`is_public=0/1` 正确，`sync_status=in_sync`
- Milvus `rag_knowledge_base` 中两个 source 的 chunk metadata 均含 `file_id` 与 `owner_id`
- 检索结果：
  - A 能检索到自己的私有文档和共享文档
  - B 检索私有文档内容返回空，检索共享文档内容能命中
  - 同一查询先 A 后 B，缓存未串用户数据

**过程中修复**：
- `api/main.py`：`MAX_UPLOAD_SIZE` 由 `os.getenv` 读到字符串导致上传接口 500，已改为 `int(...)`
- `loader/load_file.py`：`LARGE_FILE_THRESHOLD` / `MAX_FILE_SIZE` 同样存在字符串与 int 比较问题，已改为 `int(...)`

---

## 九、后续阶段（规划，未开始）

- [ ] is_public 开关接口：文档所有者修改私有/共享状态
- [x] admin 可把他人共享文档取消为私有（`POST /api/documents/{id}/revoke`）
- [x] 删除权限：owner 可删自己的
- [x] 下载接口权限：校验「owner 或 is_public=1」
- [x] 账户删除：users.status=deleting → delete_queue 队列 → 先删 Milvus → 再删 OSS → 后删 SQL → deleted
- [ ] guest 角色开放注册与权限矩阵
- [ ] 存量数据 owner_id 回填迁移脚本（现有 8 篇文档 → admin）
---

## 十、已完成：账户删除

- [x] `api/auth.py`：`get_current_user` 拒绝 `deleting/disabled` 账号；登录接口同步拒绝非 active 账号
- [x] `service/delete_queue.py`：新增 `delete_queue`（Redis Streams + 消费组 + PEL 崩溃恢复 + 指数退避重试 + 死信）
  - 先删 Milvus（按 `owner_id` 过滤）
  - 再删 OSS 原件（`delete_source_artifact` 返回是否成功，任一失败则重试，不进入 MySQL 删除）
  - 最后删除 MySQL `users` 行，`vector_files/chunk_records` 由外键级联删除
- [x] `vector_store/milvus_store.py`：新增 `delete_chunks_by_owner` / `adelete_chunks_by_owner`
- [x] `service/oss_archive.py`：`delete_source_artifact` 改为返回 `bool`，供删除队列判断 OSS 是否删除成功
- [x] `api/main.py`：新增 `POST /api/auth/delete-account`
  - 用户状态置 `deleting`
  - 该用户所有文档 `is_public=0`
  - 入 `delete_queue`；Redis 不可用时回退进程内后台任务
---

## 十一、已完成：管理员取消共享

- [x] `service/document_admin.py`：新增 `revoke_document_public`，管理员将文档 `is_public=1 → 0`
- [x] `api/main.py`：新增 `POST /api/documents/{file_id}/revoke`（仅 admin），并只清理包含该文档 source 的检索/回答缓存
- [x] `config/redis.py`：新增 `cache_clear_source(source)`，按文档 source 精准失效 `search:*` / `ans:*` 缓存
- [x] `service/chat.py`：回答缓存改为 `{answer, source_list}`，支持按 source 精准失效
- [x] 端到端验证：普通用户上传共享文档，其他用户可见；admin revoke 后 `is_public=0`，其他用户不可见，owner 自己仍可见
- [x] 端到端验证：新用户上传私有+共享文档后提交删除，状态变为 `deleting`、共享文档立即私有化；随后 Milvus 向量、OSS 原件、MySQL 用户依次删除，账号无法再登录
---

## 十二、已完成：下载接口越权修复

- [x] `api/main.py`：`download_document` 增加权限校验，仅 `owner_id=user.id` 或 `is_public=1` 可下载
- [x] 端到端验证：非 owner 访问私有文档下载返回 403，owner 下载返回 200