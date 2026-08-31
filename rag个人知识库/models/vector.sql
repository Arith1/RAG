-- ============================================
-- RAG 个人知识库 · MySQL 业务库完整表结构
-- 说明：新环境初始化直接执行本文件（users / vector_files / chunk_records / audit_logs）；
--       表结构变更一律改本文件后手动执行，应用代码不做任何 DDL。
-- 执行：mysql -u root -p rag_demo < models/vector.sql
-- ============================================

-- ============================================
-- 0. 用户表 (users) —— RBAC 权限根
-- ============================================
CREATE TABLE `users` (
    `id` BIGINT UNSIGNED NOT NULL AUTO_INCREMENT COMMENT '自增主键',
    `username` VARCHAR(64) NOT NULL COMMENT '用户名',
    `password_hash` VARCHAR(128) NOT NULL COMMENT 'bcrypt 密码哈希',
    `role` VARCHAR(16) NOT NULL DEFAULT 'user' COMMENT '角色: admin(管理员)/user(普通用户)/guest(访客,暂未开放注册)',
    `status` VARCHAR(16) NOT NULL DEFAULT 'active' COMMENT '账号状态: active(正常)/deleting(删除处理中)/deleted(已删除-软删除，数据保留)/disabled(禁用)',
    `created_at` DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '创建时间',
    `updated_at` DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP COMMENT '更新时间',

    PRIMARY KEY (`id`),
    UNIQUE KEY `uk_username` (`username`) COMMENT '用户名唯一'
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='用户表（RBAC）';

-- ============================================
-- 0.1 审计日志表 (audit_logs)
-- ============================================
CREATE TABLE `audit_logs` (
    `id` BIGINT UNSIGNED NOT NULL AUTO_INCREMENT COMMENT '自增主键',
    `user_id` BIGINT UNSIGNED NULL COMMENT '操作用户 id',
    `username` VARCHAR(64) NULL COMMENT '操作用户名（冗余，防用户删除后审计丢失）',
    `action` VARCHAR(32) NOT NULL COMMENT '操作类型: register/login_failed/upload/delete 等',
    `target` VARCHAR(512) NULL COMMENT '操作对象（文件名/路径等）',
    `detail` TEXT NULL COMMENT '补充信息',
    `created_at` DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '创建时间',
    `updated_at` DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP COMMENT '更新时间',

    PRIMARY KEY (`id`),
    KEY `idx_username` (`username`) COMMENT '按用户查审计'
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='操作审计日志';

-- ============================================
-- 1. 文件管理表 (vector_files)
-- ============================================

CREATE TABLE `vector_files` (
    `id` BIGINT UNSIGNED NOT NULL AUTO_INCREMENT COMMENT '自增主键',
    `file_name` VARCHAR(512) NOT NULL COMMENT '文件名',
    `source` VARCHAR(512) NOT NULL COMMENT '来源标识（如文件路径/URL）',
    `identity_hash` CHAR(64) NOT NULL COMMENT '文件身份唯一标识(SHA256(file_name+source))',
    `owner_id` BIGINT UNSIGNED NOT NULL COMMENT '文档归属用户 id（users.id，必填；用户删除时文档级联删除）',
    `is_public` TINYINT(1) NOT NULL DEFAULT 0 COMMENT '是否共享：0=私有(仅owner可见) 1=共享(所有登录用户可检索)',
    `file_content_hash` CHAR(64) NOT NULL COMMENT '整个文件内容的 SHA256,用来判断文件内容是否修改',
    `version` DECIMAL(5,1) NOT NULL DEFAULT 1.0 COMMENT '当前版本号（如 1.0, 2.0）',
    `chunk_count` INT UNSIGNED NOT NULL DEFAULT 0 COMMENT '该文件的 chunk 总数',
    `download_count` INT UNSIGNED NOT NULL DEFAULT 0 COMMENT '下载量（非所有者下载 +1）',
    `sync_status` VARCHAR(16) NOT NULL DEFAULT 'pending' COMMENT 'Milvus 同步状态: pending(待同步)/in_sync(一致)/failed(失败可重试)',
    `last_error` TEXT COMMENT '最近一次 Milvus 同步失败原因',
    `created_at` DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '创建时间',
    `updated_at` DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP COMMENT '更新时间',

    PRIMARY KEY (`id`),
    UNIQUE KEY `uk_identity_hash` (`identity_hash`) COMMENT '同一文件名+来源唯一',
    KEY `idx_updated_at` (`updated_at`) COMMENT '按更新时间排序',
    KEY `idx_owner_id` (`owner_id`) COMMENT '按归属用户过滤（我的文档）',
    KEY `idx_is_public` (`is_public`) COMMENT '按共享状态过滤（检索共享文档）',
    CONSTRAINT `fk_vector_files_owner` FOREIGN KEY (`owner_id`) REFERENCES `users` (`id`) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='向量文件元数据';

-- ============================================
-- 2. Chunk 记录表 (chunk_records)
-- ============================================
CREATE TABLE `chunk_records` (
    `id` BIGINT UNSIGNED NOT NULL AUTO_INCREMENT COMMENT '自增主键',
    `file_id` BIGINT UNSIGNED NOT NULL COMMENT '关联 vector_files 表的 ID',
    `chunk_fingerprint` CHAR(64) NOT NULL COMMENT 'SHA256(chunk_content + source)，同时也是 Milvus 的 ID',
    `version` DECIMAL(5,1) NOT NULL COMMENT '该 chunk 所属的版本号',
    `created_at` DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '创建时间',
    `updated_at` DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP COMMENT '更新时间',

    PRIMARY KEY (`id`),
    UNIQUE KEY `uk_chunk_fingerprint` (`chunk_fingerprint`) COMMENT 'chunk指纹唯一索引（同时是 Milvus 主键，与模型 unique=True 一致）',
    KEY `idx_file_version` (`file_id`, `version`) COMMENT '创建索引加快查找速度',
    CONSTRAINT `fk_chunk_records_file_id` FOREIGN KEY (`file_id`) REFERENCES `vector_files` (`id`) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='文件分块指纹记录';
-- ============================================
-- 3. 迁移脚本（已有数据库执行）
--    说明：新环境执行上面 CREATE TABLE 已含 download_count；
--          老库手动执行下面 ALTER 一次即可（MySQL 不支持 ADD COLUMN IF NOT EXISTS）。
--    执行：mysql -u root -p rag_demo -e "ALTER TABLE `vector_files` ADD COLUMN `download_count` INT UNSIGNED NOT NULL DEFAULT 0 COMMENT '下载量（非所有者下载 +1）' AFTER `chunk_count`;"
-- ============================================
-- ALTER TABLE `vector_files`
--     ADD COLUMN `download_count` INT UNSIGNED NOT NULL DEFAULT 0 COMMENT '下载量（非所有者下载 +1）' AFTER `chunk_count`;

-- ============================================
-- ============================================
-- 4. 会话表 (chat_sessions) —— 问答历史会话（侧边栏）
--    方案：MySQL 只存「会话列表 + 摘要」，完整消息由 Postgres（langgraph checkpoint）持有。
-- ============================================
CREATE TABLE `chat_sessions` (
    `id` BIGINT UNSIGNED NOT NULL AUTO_INCREMENT COMMENT '自增主键',
    `user_id` BIGINT UNSIGNED NOT NULL COMMENT '所属用户 id（users.id，用户删除时级联删除）',
    `session_id` VARCHAR(64) NOT NULL COMMENT '会话标识（agent 记忆 thread_id={user_id}:{session_id} 共用）',
    `title` VARCHAR(128) NOT NULL DEFAULT '新会话' COMMENT '会话标题（首问自动生成，可重命名）',
    `message_count` INT UNSIGNED NOT NULL DEFAULT 0 COMMENT '消息条数（user+assistant 都算）',
    `last_message_preview` VARCHAR(256) NOT NULL DEFAULT '' COMMENT '最后一条用户消息摘要（侧边栏展示，过长截断）',
    `last_message_at` DATETIME NULL COMMENT '最后一条消息时间（侧边栏按此倒序）',
    `retrieve_own_private` TINYINT(1) NOT NULL DEFAULT 1 COMMENT '是否检索自己的私有文档',
    `retrieve_own_public` TINYINT(1) NOT NULL DEFAULT 1 COMMENT '是否检索自己的公开文档',
    `retrieve_kb_public` TINYINT(1) NOT NULL DEFAULT 1 COMMENT '是否检索知识库里的公开文档(所有他人)',
    `created_at` DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '创建时间',
    `updated_at` DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP COMMENT '会话最后活跃/更新时间（TTL 清理依据）',

    PRIMARY KEY (`id`),
    UNIQUE KEY `uk_chat_sessions_user_session` (`user_id`, `session_id`) COMMENT '用户内会话唯一',
    KEY `idx_chat_sessions_last_message` (`user_id`, `last_message_at`) COMMENT '按用户+最近消息排序',
    KEY `idx_chat_sessions_updated_at` (`updated_at`) COMMENT 'TTL 清理按最后活跃时间扫描',
    CONSTRAINT `fk_chat_sessions_user` FOREIGN KEY (`user_id`) REFERENCES `users` (`id`) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='问答历史会话（会话元信息，完整消息在 Postgres）';

-- ============================================
-- 4.0 会话检索范围-指定用户集合 (chat_session_scope_users)
--     「指定用户的公开文档」可多选，每行存一个目标用户；
--     会话删除 / 目标用户删除时对应行级联删除。
-- ============================================
CREATE TABLE `chat_session_scope_users` (
    `id` BIGINT UNSIGNED NOT NULL AUTO_INCREMENT COMMENT '自增主键',
    `user_id` BIGINT UNSIGNED NOT NULL COMMENT '会话所属用户 id（users.id，用户删除时级联删除）',
    `session_id` VARCHAR(64) NOT NULL COMMENT '会话标识（对应 chat_sessions.session_id）',
    `target_user_id` BIGINT UNSIGNED NOT NULL COMMENT '指定检索的目标用户 id（仅检索其公开文档）',
    `created_at` DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '创建时间',
    `updated_at` DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP COMMENT '更新时间',

    PRIMARY KEY (`id`),
    UNIQUE KEY `uk_scope_session_target` (`user_id`, `session_id`, `target_user_id`) COMMENT '用户内会话+目标用户唯一',
    KEY `idx_scope_target` (`target_user_id`) COMMENT '按目标用户查（用户删除级联）',
    CONSTRAINT `fk_scope_session` FOREIGN KEY (`user_id`, `session_id`)
        REFERENCES `chat_sessions` (`user_id`, `session_id`) ON DELETE CASCADE,
    CONSTRAINT `fk_scope_target_user` FOREIGN KEY (`target_user_id`)
        REFERENCES `users` (`id`) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='会话检索范围-指定用户集合（多选，仅检索其公开文档）';

-- ============================================
-- 4.1 迁移说明（已有数据库执行）
--    新环境执行上面的 CREATE TABLE 已含检索范围字段与 chat_session_scope_users 表；
--    老库手动执行下面 SQL 一次即可：
--      1) ALTER TABLE `chat_sessions`
--             ADD COLUMN `retrieve_own_private` TINYINT(1) NOT NULL DEFAULT 1 COMMENT '是否检索自己的私有文档' AFTER `last_message_at`,
--             ADD COLUMN `retrieve_own_public` TINYINT(1) NOT NULL DEFAULT 1 COMMENT '是否检索自己的公开文档' AFTER `retrieve_own_private`,
--             ADD COLUMN `retrieve_kb_public` TINYINT(1) NOT NULL DEFAULT 1 COMMENT '是否检索知识库里的公开文档(所有他人)' AFTER `retrieve_own_public`;
--      2) CREATE TABLE `chat_session_scope_users` (
--             `id` BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
--             `user_id` BIGINT UNSIGNED NOT NULL,
--             `session_id` VARCHAR(64) NOT NULL,
--             `target_user_id` BIGINT UNSIGNED NOT NULL,
--             `created_at` DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
--             `updated_at` DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
--             PRIMARY KEY (`id`),
--             UNIQUE KEY `uk_scope_session_target` (`user_id`, `session_id`, `target_user_id`),
--             KEY `idx_scope_target` (`target_user_id`),
--             CONSTRAINT `fk_scope_session` FOREIGN KEY (`user_id`, `session_id`)
--                 REFERENCES `chat_sessions` (`user_id`, `session_id`) ON DELETE CASCADE,
--             CONSTRAINT `fk_scope_target_user` FOREIGN KEY (`target_user_id`)
--                 REFERENCES `users` (`id`) ON DELETE CASCADE
--         ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
--      3) DROP TABLE IF EXISTS `chat_messages`;  -- 旧方案遗留的消息表已废弃（完整消息改由 Postgres 持有）
--      4) （可选，清理扫描提速）ALTER TABLE `chat_sessions` ADD KEY `idx_chat_sessions_updated_at` (`updated_at`);
-- ============================================


-- ============================================
-- 5. LLM 计费表 (llm_usage) —— 每次 LLM 调用一行
--    记录意图识别 / 回答 / 摘要等所有 LLM 调用的 token 用量与预估费用。
--    同一 HTTP 请求的多次调用（intent + answer 等）通过 request_id 归组。
--    说明：user_id 不设外键，账号软删除（status=deleted）后计费记录仍保留。
-- ============================================
CREATE TABLE `llm_usage` (
    `id` BIGINT UNSIGNED NOT NULL AUTO_INCREMENT COMMENT '自增主键',

    `user_id` BIGINT UNSIGNED NOT NULL COMMENT '用户id（不设外键，账号软删除后计费记录保留）',
    `session_id` VARCHAR(64) NULL COMMENT '会话id（非聊天场景可空）',

    `request_id` VARCHAR(64) NOT NULL COMMENT '本次请求id（同一请求的多次 LLM 调用共用）',

    `provider` VARCHAR(32) NOT NULL COMMENT '模型厂商（deepseek/qwen 等）',
    `model` VARCHAR(128) NOT NULL COMMENT '模型名称',

    `type` VARCHAR(32) NOT NULL COMMENT '调用阶段: intent(意图识别)/answer(RAG回答)/chat(闲聊回答)/summarize(上下文摘要)',

    `input_tokens` INT UNSIGNED NOT NULL DEFAULT 0 COMMENT '输入令牌数（API 上报的 prompt_tokens）',
    `cached_tokens` INT UNSIGNED NOT NULL DEFAULT 0 COMMENT '命中缓存输入令牌数（低价计费）',
    `uncached_tokens` INT UNSIGNED NOT NULL DEFAULT 0 COMMENT '未命中缓存输入令牌数（原价计费）',
    `output_tokens` INT UNSIGNED NOT NULL DEFAULT 0 COMMENT '输出令牌数',
    `total_tokens` INT UNSIGNED NOT NULL DEFAULT 0 COMMENT '总令牌数',

    `credits` DECIMAL(18,6) NOT NULL DEFAULT 0 COMMENT '积分（独立体系，暂未启用，默认 0）',
    `estimated_cost` DECIMAL(18,6) NOT NULL DEFAULT 0 COMMENT '预估费用（元，按 .env 单价估算）',

    `latency_ms` INT UNSIGNED NOT NULL DEFAULT 0 COMMENT 'LLM 调用耗时（毫秒）',

    `status` VARCHAR(20) NOT NULL DEFAULT 'success' COMMENT '状态: success(成功)/failed(调用失败，费用为0)',

    `created_at` DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '创建时间',

    PRIMARY KEY (`id`),
    KEY `idx_user_time` (`user_id`, `created_at`),
    KEY `idx_request_id` (`request_id`),
    KEY `idx_provider_model` (`provider`, `model`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='LLM 调用计费记录';

-- ============================================
-- 5.1 迁移说明（已有数据库执行）
--    新环境执行上面的 CREATE TABLE 已建好 llm_usage；
--    老库把上方 CREATE TABLE 语句整体执行一次即可（表当前尚未创建）。
-- ============================================

-- ============================================
-- 6. RAG 请求链路追踪表 (rag_traces) —— 每次问答请求一行
--    记录 意图识别 → 检索 → 精排 → 生成 各阶段指标与耗时；
--    与 llm_usage 共用 request_id，便于关联费用与链路。
-- ============================================
CREATE TABLE `rag_traces` (
    `id` BIGINT UNSIGNED NOT NULL AUTO_INCREMENT COMMENT '自增主键',

    `request_id` VARCHAR(64) NOT NULL COMMENT '本次请求id（与 llm_usage.request_id 一致）',
    `user_id` BIGINT UNSIGNED NOT NULL COMMENT '用户id',
    `session_id` VARCHAR(64) NULL COMMENT '会话id',

    `intent` VARCHAR(32) NULL COMMENT '意图识别结果: rag_ask/chat/other 等',
    `query` VARCHAR(1024) NULL COMMENT '用户提问/检索文本（便于排查）',
    `status` VARCHAR(20) NOT NULL DEFAULT 'success' COMMENT '状态: success/failed',
    `error_type` VARCHAR(64) NULL COMMENT '错误类型',
    `error_message` VARCHAR(512) NULL COMMENT '错误信息',

    `total_ms` INT UNSIGNED NOT NULL DEFAULT 0 COMMENT '端到端耗时(毫秒)',
    `intent_ms` INT UNSIGNED NOT NULL DEFAULT 0 COMMENT '意图识别耗时',
    `retrieval_ms` INT UNSIGNED NOT NULL DEFAULT 0 COMMENT '检索耗时',
    `retrieval_cache_hit` TINYINT(1) NOT NULL DEFAULT 0 COMMENT '检索缓存是否命中',
    `retrieval_has_scope` TINYINT(1) NOT NULL DEFAULT 1 COMMENT '是否有可见文档',
    `recall_count` INT UNSIGNED NOT NULL DEFAULT 0 COMMENT '双路召回候选数',
    `rerank_count` INT UNSIGNED NOT NULL DEFAULT 0 COMMENT '精排后命中数',
    `rerank_avg_score` DECIMAL(6,4) NULL COMMENT '精排平均分',
    `rerank_max_score` DECIMAL(6,4) NULL COMMENT '精排最高分',
    `rerank_degraded` TINYINT(1) NOT NULL DEFAULT 0 COMMENT '精排是否降级(接口失败)',
    `generation_ms` INT UNSIGNED NOT NULL DEFAULT 0 COMMENT 'LLM 生成耗时',
    `answer_len` INT UNSIGNED NOT NULL DEFAULT 0 COMMENT '回答字符数',

    `sources` JSON NULL COMMENT '来源列表 [{source, score}]',
    `created_at` DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '创建时间',

    PRIMARY KEY (`id`),
    KEY `idx_trace_request` (`request_id`),
    KEY `idx_trace_user_time` (`user_id`, `created_at`),
    KEY `idx_trace_status_time` (`status`, `created_at`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='RAG 请求链路追踪';

-- 6.1 迁移说明（已有数据库执行）：把上方 CREATE TABLE 语句整体执行一次即可。