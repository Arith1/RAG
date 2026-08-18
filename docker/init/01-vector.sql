-- MySQL 初始化入口：先选中库，再执行挂载的 models/vector.sql（建 4 张业务表）
-- 该文件由 mysql 镜像的 /docker-entrypoint-initdb.d 自动执行（仅首次初始化）
USE rag_demo;
SOURCE /init-sql/vector.sql;
