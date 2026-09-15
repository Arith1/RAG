-- 应用启动时会自动创建并登记当前 schema 版本；本文件供 DBA 手工初始化/审计。
CREATE TABLE IF NOT EXISTS `schema_migrations` (
    `version` INT NOT NULL COMMENT 'schema 版本号',
    `name` VARCHAR(128) NOT NULL COMMENT '迁移名称',
    `applied_at` DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '应用时间',
    PRIMARY KEY (`version`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='数据库 schema 版本记录';

-- 当前基线版本；已有完整 schema 的数据库可执行一次。
INSERT IGNORE INTO `schema_migrations` (`version`, `name`)
VALUES (1, 'baseline_schema_contract');
