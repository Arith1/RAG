-- ============================================
-- 对话记忆（Postgres）表结构变更
-- ============================================
-- 背景：
--   langgraph-checkpoint-postgres 会自动创建 checkpoints / checkpoint_writes /
--   checkpoint_blobs 三张表（由 saver.setup() 完成），本文件只负责 langgraph
--   表结构之外的补充变更，手动执行一次即可（幂等，可重复执行）。
--
-- 执行方式：
--   psql -U <user> -d <db> -f models/postgres_memory.sql
--   （或 pgAdmin / Docker exec 中执行下面的语句）
--
-- 用途：
--   TTL 清理已改为以 MySQL chat_sessions.updated_at 为基准（见 service/memory_maintenance.py），
--   checkpoints 不再需要 created_at 列。下面的 ALTER 仅保留用于兼容/排查历史数据：
--   新环境可跳过；已加列的环境也可安全保留（langgraph 不写该列，仅占一列）。

-- 1. checkpoints 补充 created_at（TTL 清理的时间依据）
ALTER TABLE checkpoints
    ADD COLUMN IF NOT EXISTS created_at TIMESTAMPTZ DEFAULT now();

-- 2.（可选）清理查询按 created_at 过滤，数据量大时建议建索引
-- CREATE INDEX IF NOT EXISTS idx_checkpoints_created_at
--     ON checkpoints (created_at);
