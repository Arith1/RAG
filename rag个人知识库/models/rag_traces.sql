-- RAG 请求链路追踪表：执行一次即可（IF NOT EXISTS，重复执行安全）
CREATE TABLE IF NOT EXISTS `rag_traces` (
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
    `trace_type` VARCHAR(16) NULL COMMENT '来源类型: chat/search'
    `query_raw` VARCHAR(1024) NULL COMMENT '原始输入（未提炼）'
    `embedding_ms` INT UNSIGNED NOT NULL DEFAULT 0 COMMENT 'embedding 耗时(毫秒)'
    `milvus_ms` INT UNSIGNED NOT NULL DEFAULT 0 COMMENT 'Milvus 召回耗时(毫秒)'
    `rerank_ms` INT UNSIGNED NOT NULL DEFAULT 0 COMMENT 'rerank 耗时(毫秒)'
    `cache_ms` INT UNSIGNED NOT NULL DEFAULT 0 COMMENT '缓存读写耗时(毫秒)'
    `created_at` DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '创建时间',
    PRIMARY KEY (`id`),
    KEY `idx_trace_request` (`request_id`),
    KEY `idx_trace_user_time` (`user_id`, `created_at`),
    KEY `idx_trace_status_time` (`status`, `created_at`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='RAG 请求链路追踪';
