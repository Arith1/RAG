from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession

from rag个人知识库.models.vector import Base

# 数据库url
ASYNC_DATABASE_URL = "mysql+aiomysql://root:root@localhost:3306/rag_demo?charset=utf8"
# 1.创建异步引擎
engine = create_async_engine(
    ASYNC_DATABASE_URL,
    echo =  True, # 输出日志
    pool_size = 10, # 连接池大小
    max_overflow = 20, # 溢出连接池大小

)
# 2.创建异步会话工厂，用于创建会话对象
AsyncSession = async_sessionmaker(
    bind=engine, # 绑定引擎
    class_=AsyncSession, # 使用的会话类
    expire_on_commit=False, # 会话对象不过期不重新查询数据库
    # autoflush=False, # 自动刷新
    # future=True, # 启用未来对象
    # connect_args={"check_same_thread": False}
)
# 3.依赖项，用于创建会话对象
async def get_db():
    async with AsyncSession() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
        finally:
            await session.close()
        # 因为有with所以不try except也可以


async def init_db():
    """建表（幂等）+ 增量列迁移：首次运行或表结构变更后执行"""
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        await conn.run_sync(_migrate_columns)


def _migrate_columns(conn):
    """为已存在的旧表补充新增列（create_all 不会修改已存在的表）"""
    from sqlalchemy import inspect, text

    inspector = inspect(conn)
    cols = (
        {c["name"] for c in inspector.get_columns("vector_files")}
        if inspector.has_table("vector_files") else set()
    )

    def add_col(column: str, ddl: str) -> None:
        if column not in cols:
            conn.execute(text(f"ALTER TABLE vector_files ADD COLUMN {ddl}"))

    add_col(
        "sync_status",
        "sync_status VARCHAR(16) NOT NULL DEFAULT 'pending' COMMENT 'Milvus同步状态'",
    )
    add_col(
        "last_error",
        "last_error TEXT NULL COMMENT '最近一次Milvus同步失败原因'",
    )