"""
通用数据库工具（异步）

此模块提供基于 SQLAlchemy 2.x 异步引擎的数据库访问封装，支持 MySQL 与 PostgreSQL。
数据模型定义位置：
- 无（本模块仅提供连接与查询工具，不定义数据模型）
"""

from __future__ import annotations
from urllib.parse import quote_plus
import asyncio
import os
from typing import Any, Dict, Iterable, List, Optional, Union

from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, create_async_engine
from sqlalchemy import text
from InsightEngine.utils.config import settings

__all__ = [
    "get_async_engine",
    "fetch_all",
    "execute_write",
    "execute_write_many",
    "_run_async"
]


_engine: Optional[AsyncEngine] = None

def _run_async(coro):
    """安全的异步执行包装器，兼容多线程与事件循环环境"""
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None
        
    if loop and loop.is_running():
        import concurrent.futures
        with concurrent.futures.ThreadPoolExecutor(1) as pool:
            return pool.submit(asyncio.run, coro).result()
    else:
        return asyncio.run(coro)


def _build_database_url() -> str:
    dialect: str = (settings.DB_DIALECT or "mysql").lower()
    host: str = settings.DB_HOST or ""
    port: str = str(settings.DB_PORT or "")
    
    # 自动探测 Docker 环境，修正本地回环地址
    if os.path.exists("/.dockerenv") and host in ("localhost", "127.0.0.1"):
        host = "db"
        if port == "5444":
            port = "5432"

    user: str = settings.DB_USER or ""
    password: str = settings.DB_PASSWORD or ""
    db_name: str = settings.DB_NAME or ""

    if os.getenv("DATABASE_URL"):
        return os.getenv("DATABASE_URL")  # 直接使用外部提供的完整URL

    password = quote_plus(password)

    if dialect in ("postgresql", "postgres"):
        # PostgreSQL 使用 asyncpg 驱动
        return f"postgresql+asyncpg://{user}:{password}@{host}:{port}/{db_name}"

    # 默认 MySQL 使用 aiomysql 驱动
    return f"mysql+aiomysql://{user}:{password}@{host}:{port}/{db_name}"


def get_async_engine() -> AsyncEngine:
    global _engine
    if _engine is None:
        database_url: str = _build_database_url()
        _engine = create_async_engine(
            database_url,
            pool_pre_ping=True,
            pool_recycle=1800,
        )
    return _engine


async def fetch_all(query: str, params: Optional[Union[Iterable[Any], Dict[str, Any]]] = None) -> List[Dict[str, Any]]:
    """
    执行只读查询并返回字典列表。
    """
    engine: AsyncEngine = get_async_engine()
    async with engine.connect() as conn:
        result = await conn.execute(text(query), params or {})
        rows = result.mappings().all()
        # 将 RowMapping 转换为普通字典
        return [dict(row) for row in rows]

async def execute_write(query: str, params: Optional[Union[Iterable[Any], Dict[str, Any]]] = None) -> int:
    """
    执行写操作（INSERT/UPDATE/DELETE）并返回受影响的行数。
    """
    engine: AsyncEngine = get_async_engine()
    async with engine.begin() as conn:
        result = await conn.execute(text(query), params or {})
        return result.rowcount

async def execute_write_many(query: str, params: List[Dict[str, Any]]) -> int:
    """
    批量执行写操作（INSERT/UPDATE/DELETE）以极大提升性能。
    """
    if not params:
        return 0
    engine: AsyncEngine = get_async_engine()
    async with engine.begin() as conn:
        result = await conn.execute(text(query), params)
        return result.rowcount


