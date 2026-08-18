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
--   为 checkpoints 表补充 created_at 列（默认 now()）。langgraph 每次写入
--   checkpoint 都会新建一行并自动带上时间，因此 max(created_at) 即该会话线程的
--   "最后一次活跃时间"，TTL 清理以它为基准倒计时（默认 24 小时）。

-- 1. checkpoints 补充 created_at（TTL 清理的时间依据）
ALTER TABLE checkpoints
    ADD COLUMN IF NOT EXISTS created_at TIMESTAMPTZ DEFAULT now();

-- 2.（可选）清理查询按 created_at 过滤，数据量大时建议建索引
-- CREATE INDEX IF NOT EXISTS idx_checkpoints_created_at
--     ON checkpoints (created_at);
