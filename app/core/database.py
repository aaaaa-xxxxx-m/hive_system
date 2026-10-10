from contextlib import asynccontextmanager

from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession, async_sessionmaker
from sqlalchemy.orm import DeclarativeBase, declared_attr
from sqlalchemy.pool import NullPool
from sqlalchemy import MetaData
from app.core.config import get_settings

settings = get_settings()

engine = create_async_engine(
    settings.DATABASE_URL,
    echo=settings.DEBUG,
    pool_pre_ping=True,
    pool_recycle=3600,
    pool_size=10,
    max_overflow=20,
)

async_session_maker = async_sessionmaker(
    engine,
    class_=AsyncSession,
    expire_on_commit=False,
    autocommit=False,
    autoflush=False,
)

convention = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}

metadata = MetaData(naming_convention=convention)


class Base(DeclarativeBase):
    metadata = metadata

    @declared_attr.directive
    def __tablename__(cls) -> str:
        return cls.__name__.lower()

    def to_dict(self) -> dict:
        return {c.name: getattr(self, c.name) for c in self.__table__.columns}


async def get_db() -> AsyncSession:
    async with async_session_maker() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
        finally:
            await session.close()


async def init_db() -> None:
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


async def close_db() -> None:
    await engine.dispose()


@asynccontextmanager
async def task_session():
    """Celery 一次性任务专用会话：每次新建 NullPool 引擎，用完即弃。

    Celery 任务用 ``asyncio.run()``，每次都会新建事件循环；若复用模块级带连接池的
    ``engine``/``async_session_maker``，连接池里来自旧事件循环的连接会被再次取出，
    触发 ``Future attached to a different loop`` / ``Event loop is closed``。
    这里改用 NullPool（不跨循环缓存连接）并在结束时 dispose，从根本上规避该问题。
    """
    eng = create_async_engine(settings.DATABASE_URL, poolclass=NullPool)
    try:
        Session = async_sessionmaker(
            eng, class_=AsyncSession, expire_on_commit=False,
            autocommit=False, autoflush=False,
        )
        async with Session() as session:
            yield session
    finally:
        await eng.dispose()