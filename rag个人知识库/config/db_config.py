from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession

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
