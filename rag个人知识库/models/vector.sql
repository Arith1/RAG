-- ============================================
-- 1. 文件管理表 (vector_files)
-- ============================================

CREATE TABLE `vector_files` (
    `id` BIGINT UNSIGNED NOT NULL AUTO_INCREMENT COMMENT '自增主键',
    `file_name` VARCHAR(256) NOT NULL COMMENT '文件名',
    `source` VARCHAR(256) NOT NULL COMMENT '来源标识（如文件路径/URL）',
    `identity_hash` CHAR(64) NOT NULL COMMENT '文件身份唯一标识(SHA256(file_name+source))',
    `file_content_hash` CHAR(64) NOT NULL COMMENT '整个文件内容的 SHA256,用来判断文件内容是否修改',
    `version` DECIMAL(5,1) NOT NULL DEFAULT 1.0 COMMENT '当前版本号（如 1.0, 2.0）',
    `chunk_count` INT UNSIGNED NOT NULL DEFAULT 0 COMMENT '该文件的 chunk 总数',
    `sync_status` VARCHAR(16) NOT NULL DEFAULT 'pending' COMMENT 'Milvus 同步状态: pending(待同步)/in_sync(一致)/failed(失败可重试)',
    `last_error` TEXT COMMENT '最近一次 Milvus 同步失败原因',
    `created_at` DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '创建时间',
    `updated_at` DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP COMMENT '更新时间',

    PRIMARY KEY (`id`),
    UNIQUE KEY `uk_identity_hash` (`identity_hash`) COMMENT '同一文件名+来源唯一',
    KEY `idx_updated_at` (`updated_at`) COMMENT '按更新时间排序'
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