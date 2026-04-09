"""
专为 AI Agent 设计的本地舆情数据库查询工具集 (原 Bocha/Anspire 接口)

版本: 2.0
最后更新: 2025-08-23

此脚本已重构为直接查询本地 MySQL 数据库，不再依赖外部的 Bocha 或 Anspire API，
以解决 API 调用成本高、请求频繁的问题。
同时保持了原有数据结构 (BochaResponse, WebpageResult 等) 的兼容性，
使得 MediaEngine/agent.py 无需修改即可无缝切换到本地数据库。
"""

import os
import json
import sys
import datetime
import asyncio
import concurrent.futures
from typing import List, Dict, Any, Optional, Literal
from dataclasses import dataclass, field

from loguru import logger
from ..utils.config import settings

# 添加utils目录到Python路径

# 导入共享的数据库工具
try:
    from InsightEngine.utils.db import fetch_all
except ImportError:
    # 兼容直接运行测试
    sys.path.append(root_dir)
    from InsightEngine.utils.db import fetch_all

def _run_async(coro):
    """安全的异步执行包装器"""
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None
        
    if loop and loop.is_running():
        with concurrent.futures.ThreadPoolExecutor(1) as pool:
            return pool.submit(asyncio.run, coro).result()
    else:
        return asyncio.run(coro)

# --- 1. 数据结构定义 (保持兼容) ---

@dataclass
class WebpageResult:
    """网页搜索结果"""
    name: str
    url: str
    snippet: str
    display_url: Optional[str] = None
    date_last_crawled: Optional[str] = None

@dataclass
class ImageResult:
    """图片搜索结果"""
    name: str
    content_url: str
    host_page_url: Optional[str] = None
    thumbnail_url: Optional[str] = None
    width: Optional[int] = None
    height: Optional[int] = None

@dataclass
class ModalCardResult:
    """模态卡结构化数据结果"""
    card_type: str
    content: Dict[str, Any]

@dataclass
class BochaResponse:
    """封装搜索结果，兼容 Bocha API 结构"""
    query: str
    conversation_id: Optional[str] = None
    answer: Optional[str] = None
    follow_ups: List[str] = field(default_factory=list)
    webpages: List[WebpageResult] = field(default_factory=list)
    images: List[ImageResult] = field(default_factory=list)
    modal_cards: List[ModalCardResult] = field(default_factory=list)

@dataclass
class AnspireResponse:
    """封装搜索结果，兼容 Anspire API 结构"""
    query: str
    conversation_id: Optional[str] = None
    score: Optional[float] = None
    webpages: List[WebpageResult] = field(default_factory=list)


# --- 2. 核心客户端与专用工具集 (本地数据库版) ---

class LocalDatabaseSearch:
    """
    本地数据库搜索核心类。
    实现通用的 SQL 查询逻辑，供 BochaMultimodalSearch 和 AnspireAISearch 调用。
    """
    
    @staticmethod
    def _parse_timestamp(ts: Any) -> Optional[str]:
        if not ts: return None
        try:
            if isinstance(ts, datetime.datetime):
                return ts.strftime("%Y-%m-%d %H:%M:%S")
            if isinstance(ts, str) and ts.isdigit():
                ts = int(ts)
            if isinstance(ts, int):
                if ts > 1e11:  # 13位毫秒时间戳
                    return datetime.datetime.fromtimestamp(ts / 1000).strftime("%Y-%m-%d %H:%M:%S")
                return datetime.datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S")
            if isinstance(ts, str):
                return ts
        except Exception:
            return None
        return None

    def _safe_query(self, sql: str, params: dict) -> List[dict]:
        try:
            return _run_async(fetch_all(sql, params))
        except Exception as e:
            logger.debug(f"查询本地库出错或表不存在: {e}")
            return []

    def _search_local_db(self, query: str, limit: int = 10, start_ts: int = 0, end_ts: int = 0) -> List[WebpageResult]:
        return _run_async(self._search_local_db_async(query, limit, start_ts, end_ts))

    async def _search_local_db_async(self, query: str, limit: int = 10, start_ts: int = 0, end_ts: int = 0) -> List[WebpageResult]:
        """
        执行跨表联合查询，返回标准化的 WebpageResult 列表
        """
        topic_like = f"%{query}%"
        params = {"topic": topic_like, "limit": limit}
        
        time_filter = ""
        if start_ts > 0 and end_ts > 0:
            time_filter = " AND {time_col} >= :start_ts AND {time_col} <= :end_ts"
            params["start_ts"] = start_ts
            params["end_ts"] = end_ts
        elif start_ts > 0:
            time_filter = " AND {time_col} >= :start_ts"
            params["start_ts"] = start_ts
            
        queries = [
            ("seed", f"SELECT title, description as content, add_ts as time, url FROM daily_news WHERE source_platform = 'seed_document' AND (title LIKE :topic OR description LIKE :topic){time_filter.format(time_col='add_ts')} ORDER BY add_ts DESC LIMIT :limit"),
            ("xhs", f"SELECT title, \"desc\" as content, time, note_url as url FROM xhs_note WHERE (title LIKE :topic OR \"desc\" LIKE :topic){time_filter.format(time_col='time')} ORDER BY time DESC LIMIT :limit"),
            ("bilibili", f"SELECT title, \"desc\" as content, create_time as time, video_url as url FROM bilibili_video WHERE (title LIKE :topic OR \"desc\" LIKE :topic){time_filter.format(time_col='create_time')} ORDER BY create_time DESC LIMIT :limit"),
            ("douyin", f"SELECT title, \"desc\" as content, create_time as time, aweme_url as url FROM douyin_aweme WHERE (title LIKE :topic OR \"desc\" LIKE :topic){time_filter.format(time_col='create_time')} ORDER BY create_time DESC LIMIT :limit"),
            ("weibo", f"SELECT content as title, content as content, create_time as time, note_url as url FROM weibo_note WHERE (content LIKE :topic){time_filter.format(time_col='create_time')} ORDER BY create_time DESC LIMIT :limit"),
            ("zhihu", f"SELECT title, content_text as content, created_time as time, content_url as url FROM zhihu_content WHERE (title LIKE :topic OR content_text LIKE :topic){time_filter.format(time_col='created_time')} ORDER BY created_time DESC LIMIT :limit"),
            ("tieba", f"SELECT title, \"desc\" as content, add_ts as time, note_url as url FROM tieba_note WHERE (title LIKE :topic OR \"desc\" LIKE :topic){time_filter.format(time_col='add_ts')} ORDER BY add_ts DESC LIMIT :limit"),
            ("daily_news", f"SELECT title, description as content, add_ts as time, url FROM daily_news WHERE (title LIKE :topic OR description LIKE :topic){time_filter.format(time_col='add_ts')} ORDER BY add_ts DESC LIMIT :limit")
        ]
        
        results = []
        try:
            tasks = [fetch_all(sql, params) for _, sql in queries]
            all_rows = await asyncio.gather(*tasks, return_exceptions=True)
            for i, rows in enumerate(all_rows):
                if isinstance(rows, Exception):
                    logger.debug(f"查询本地库出错或表不存在: {rows}")
                    continue
                platform = queries[i][0]
                for r in rows:
                    title = r.get('title', '')
                    content = r.get('content', '')
                    time_val = r.get('time')
                    url = r.get('url')
                    
                    results.append(WebpageResult(
                        name=f"[{platform.upper()}] {title}" if title else f"[{platform.upper()}] 网友讨论",
                        url=url or f"local://{platform}/{time_val}",
                        snippet=content,
                        date_last_crawled=self._parse_timestamp(time_val)
                    ))
        finally:
            import InsightEngine.utils.db as db_utils
            db_utils._engine = None  # Clear global engine to prevent event loop issues
        
        # 按照时间降序排序，并截取前 limit 个
        results.sort(key=lambda x: x.date_last_crawled or "", reverse=True)
        return results[:limit]


class BochaMultimodalSearch(LocalDatabaseSearch):
    """
    兼容原 Bocha API 的接口，底层切换为本地数据库查询。
    """
    def __init__(self, api_key: Optional[str] = None):
        pass # 忽略 API Key，使用本地库

    def comprehensive_search(self, query: str, max_results: int = 10) -> BochaResponse:
        logger.info(f"--- TOOL: 全面综合搜索 (本地DB) (query: {query}) ---")
        results = self._search_local_db(query, limit=max_results)
        return BochaResponse(query=query, webpages=results, answer="（本地数据库检索，不提供总结）")

    def web_search_only(self, query: str, max_results: int = 15) -> BochaResponse:
        logger.info(f"--- TOOL: 纯网页搜索 (本地DB) (query: {query}) ---")
        results = self._search_local_db(query, limit=max_results)
        return BochaResponse(query=query, webpages=results)

    def search_for_structured_data(self, query: str) -> BochaResponse:
        logger.info(f"--- TOOL: 结构化数据查询 (本地DB) (query: {query}) ---")
        results = self._search_local_db(query, limit=5)
        return BochaResponse(query=query, webpages=results)

    def search_last_24_hours(self, query: str) -> BochaResponse:
        logger.info(f"--- TOOL: 搜索24小时内信息 (本地DB) (query: {query}) ---")
        start_ts = int((datetime.datetime.now() - datetime.timedelta(days=1)).timestamp() * 1000)
        results = self._search_local_db(query, limit=15, start_ts=start_ts)
        return BochaResponse(query=query, webpages=results)

    def search_last_week(self, query: str) -> BochaResponse:
        logger.info(f"--- TOOL: 搜索本周信息 (本地DB) (query: {query}) ---")
        start_ts = int((datetime.datetime.now() - datetime.timedelta(weeks=1)).timestamp() * 1000)
        results = self._search_local_db(query, limit=15, start_ts=start_ts)
        return BochaResponse(query=query, webpages=results)


class AnspireAISearch(LocalDatabaseSearch):
    """
    兼容原 Anspire API 的接口，底层切换为本地数据库查询。
    """
    def __init__(self, api_key: Optional[str] = None):
        pass

    def comprehensive_search(self, query: str, max_results: int = 10) -> AnspireResponse:
        logger.info(f"--- TOOL: 综合搜索 (本地DB) (query: {query}) ---")
        results = self._search_local_db(query, limit=max_results)
        return AnspireResponse(query=query, webpages=results)

    def search_last_24_hours(self, query: str, max_results: int = 10) -> AnspireResponse:
        logger.info(f"--- TOOL: 搜索24小时内信息 (本地DB) (query: {query}) ---")
        start_ts = int((datetime.datetime.now() - datetime.timedelta(days=1)).timestamp() * 1000)
        results = self._search_local_db(query, limit=max_results, start_ts=start_ts)
        return AnspireResponse(query=query, webpages=results)

    def search_last_week(self, query: str, max_results: int = 10) -> AnspireResponse:
        logger.info(f"--- TOOL: 搜索本周信息 (本地DB) (query: {query}) ---")
        start_ts = int((datetime.datetime.now() - datetime.timedelta(weeks=1)).timestamp() * 1000)
        results = self._search_local_db(query, limit=max_results, start_ts=start_ts)
        return AnspireResponse(query=query, webpages=results)

# --- 3. 测试与使用示例 ---
def print_response_summary(response):
    if not response or not response.query:
        logger.error("未能获取有效响应。")
        return

    logger.info(f"\n查询: '{response.query}'")
    logger.info(f"找到 {len(response.webpages)} 个结果")

    if response.webpages:
        for idx, result in enumerate(response.webpages[:5], 1):
            logger.info(f" {idx}. {result.name}")
            logger.info(f"    {result.snippet[:50]}...")
            logger.info(f"    [{result.date_last_crawled}] {result.url}")

    logger.info("-" * 60)

if __name__ == "__main__":
    search_client = BochaMultimodalSearch()
    response1 = search_client.comprehensive_search(query="人工智能对未来教育的影响")
    print_response_summary(response1)
