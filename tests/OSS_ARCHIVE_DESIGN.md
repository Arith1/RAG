# 文档上传 → 入库 → OSS 归档 → 删本地 → 下载 链路说明

> 目标：服务器不长期占用磁盘存储用户上传的原始文档——上传的文档先落到本地 `uploads/` **临时中转**，入库成功后归档到**阿里云 OSS**，归档成功才删除本地原件；用户要下载源文档时，通过 OSS 链接（或本地副本）取回。

---

## 一、链路全景

```
用户(A) 上传 a.md
   │
   ▼
uploads/{userId}/a.md        ← 按用户分目录，临时中转
   │   进入 Redis 入库队列（ingest_queue）
   ▼
worker 入库（加载/切分/MySQL/Milvus）
   │
   ├── 入库失败 ──► 重试（此时本地 a.md 仍在，重试直接用）
   │
   └── 入库成功 ──► 归档到 OSS（key = uploads/{userId}/a.md）
                        │
                        ├── 归档成功 ──► 删除本地 uploads/{userId}/a.md
                        └── 归档失败 ──► 保留本地，重试归档
```

**核心时序保证**：删除本地原件，只发生在「入库 + OSS 归档都成功」之后。任何中途失败，本地原件都保留，可重试。

---

## 二、关键约定

### 1. source 统一为相对路径
- 上传落盘 `uploads/{userId}/{file_name}`，按用户隔离，同名不覆盖。
- `source`（库内 / Milvus 主键 / 检索溯源）统一为相对路径 `uploads/{userId}/{file}`。
- **OSS 对象 key = 相对路径**，下载 URL = `OSS_PUBLIC_BASE + 相对路径`（无需额外映射表）。
- 非 API 上传场景（CLI 手动入库）`source` 保持原路径不变，不影响既有指纹口径。

### 2. 删除时机（原件保管关键）
```python
# worker 成功路径（service/ingest_queue.py）
result = await ingest_files([path])
if 入库结果里有 error:   return False        # 重试，原件保留
archived = await archive_local_file(path)     # 归档 OSS
return archived                               # False→归档失败也重试，原件保留
```

### 3. 下载
- OSS 启用 → 返回**签名 URL**（私有桶）或公有 URL。
- OSS 未启用但本地原件还在 → 直接**流式返回本地文件**（降级可用）。

---

## 三、改动文件

| 文件 | 说明 |
| --- | --- |
| `rag个人知识库/service/oss_archive.py` | **新增**：OSS 配置、归档、删除、签名/下载 URL、相对路径工具、未启用时降级保护 |
| `rag个人知识库/service/ingest.py` | `source` 相对化（precheck / insert / process_file 三处口径一致） |
| `rag个人知识库/service/ingest_queue.py` | worker 成功后归档 OSS + 删本地；失败重试；inflight 存相对 source |
| `rag个人知识库/service/document_admin.py` | 删除文档联动删 OSS + 本地原件 |
| `rag个人知识库/api/main.py` | 上传按用户分目录；`_sanitize_source` 支持相对路径；新增下载接口 |
| `pyproject.toml` | 新增 `oss2` 依赖 |
| `.env.example` | 新增 OSS 配置项 |

---

## 四、OSS 配置（.env）

```ini
# 不配置则保持本地保留（不删原件），不影响功能
OSS_ENABLE=false
OSS_ENDPOINT=https://oss-cn-hangzhou.aliyuncs.com
OSS_ACCESS_KEY_ID=your_access_key_id
OSS_ACCESS_KEY_SECRET=your_access_key_secret
OSS_BUCKET=your-bucket-name
# 公有读桶填 https://{bucket}.{endpoint} 或 CDN 域名；私有桶留空（走签名 URL）
OSS_PUBLIC_BASE=
```

> 建议使用**私有桶 + 签名 URL**；AccessKey 只放服务端 `.env`，绝不下发前端。

---

## 五、新增接口

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| `GET` | `/api/documents/{id}/download` | 下载源文档：OSS 签名/公有 URL，或本地流式返回 |

---

## 六、验证情况

已在本地端到端验证（OSS 未启用 → 本地保留降级路径）：

- 上传（admin）→ 落盘 `uploads/1/xxx.md`（按用户目录）✓
- 异步入库 → `in_sync` ✓
- `source` → `uploads/1/xxx.md`（相对路径）✓
- 下载 → HTTP 200 返回正确内容 ✓
- 删除 → HTTP 200 且本地原件被清理 ✓

> 真实 OSS 传输 / 签名 URL 分支需配置好 `.env` 后在能访问 OSS 的环境验证。

---

## 七、注意点

1. **存量数据**：库内既有文档 `source` 为绝对路径（旧口径），仅影响新上传；存量若需统一到相对路径 + OSS 归档，需做一次迁移（改写 source + 重算指纹重灌 Milvus）。
2. **归档失败重试**：`archive_local_file` 返回 False 时任务走重试，本地原件保留，直到归档成功。
3. **孤儿文件**：极少数「入库成功但归档长期失败」的 upload 残留，可定期扫描补归档或告警。
4. **日志**：所有归档/删除均走 `logging`（模块 `oss_archive`），便于排查。
