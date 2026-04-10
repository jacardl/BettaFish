"""
专为 AI Agent 设计的舆情搜索工具集 (本地数据库)

版本: 2.0
最后更新: 2025-08-22

此脚本将搜索功能重构为从本地 SQLite 数据库中检索数据，
保持了与原有 Tavily 接口相同的签名和返回结构，以便 Agent 无缝切换。

新特性:
- 彻底移除 Tavily 依赖，改为检索本地每日新闻及社媒表。
- 提取 extra_info 中的多模态数据。
"""

import os
import sys
import datetime
import asyncio
from typing import List, Dict, Any, Optional

# 添加utils目录到Python路径
current_dir = os.path.dirname(os.path.abspath(__file__))
root_dir = os.path.dirname(os.path.dirname(current_dir))
utils_dir = os.path.join(root_dir, 'utils')
if utils_dir not in sys.path:
    sys.path.append(utils_dir)

from retry_helper import with_graceful_retry, SEARCH_API_RETRY_CONFIG
from dataclasses import dataclass, field

# 引入本地数据库查询工具
sys.path.insert(0, root_dir)
from InsightEngine.utils.db import fetch_all, _run_async
import logging

logger = logging.getLogger(__name__)

# --- 1. 数据结构定义 ---

@dataclass
class SearchResult:
    """
    网页搜索结果数据类
    包含 published_date 属性来存储新闻发布日期
    """
    title: str
    url: str
    content: str
    score: Optional[float] = None
    raw_content: Optional[str] = None
    published_date: Optional[str] = None

@dataclass
class ImageResult:
    """图片搜索结果数据类"""
    url: str
    description: Optional[str] = None

@dataclass
class TavilyResponse:
    """封装搜索 API 的完整返回结果，保持原有命名以便兼容"""
    query: str
    answer: Optional[str] = None
    results: List[SearchResult] = field(default_factory=list)
    images: List[ImageResult] = field(default_factory=list)
    response_time: Optional[float] = None


# --- 2. 核心客户端与专用工具集 ---

class TavilyNewsAgency:
    """
    一个包含多种专用新闻舆情搜索工具的客户端。
    底层已重构为本地数据库查询。
    """

    def __init__(self, api_key: Optional[str] = None):
        """初始化客户端（API Key参数保留以兼容旧代码，但不再使用）"""
        pass

    def _build_keyword_conditions(self, topic: str, columns: List[str]) -> Tuple[str, dict]:
        """
        将用户的搜索词拆分为多个关键词，并构建 AND 模糊匹配条件。
        """
        keywords = [k.strip() for k in topic.replace('+', ' ').split() if k.strip()][:5]
        if not keywords:
            keywords = [topic]
            
        conditions = []
        params = {}
        for i, kw in enumerate(keywords):
            kw_key = f"kw_{i}"
            params[kw_key] = f"%{kw}%"
            col_conds = [f"{col} LIKE :{kw_key}" for col in columns]
            conditions.append("(" + " OR ".join(col_conds) + ")")
            
        return " AND ".join(conditions), params

    def _parse_timestamp(self, ts) -> str:
        """将毫秒时间戳转换为 YYYY-MM-DD HH:MM:SS 格式"""
        if not ts:
            return ""
        try:
            return datetime.datetime.fromtimestamp(int(ts) / 1000).strftime('%Y-%m-%d %H:%M:%S')
        except Exception:
            return str(ts)

    async def _search_local_db_async(self, query: str, limit: int = 10, start_ts: int = 0, end_ts: int = 0):
        """异步查询本地数据库的各个表"""
        time_filter = ""
        time_params = {}
        if start_ts > 0:
            time_params["start_ts"] = start_ts
            time_filter += " AND {time_col} >= :start_ts"
        if end_ts > 0:
            time_params["end_ts"] = end_ts
            time_filter += " AND {time_col} <= :end_ts"
            
        table_config = {
            "seed": ("daily_news", "add_ts", ["title", "description"], "source_platform = 'seed_document'"),
            "xhs": ("xhs_note", "time", ["title", "\"desc\""], None),
            "bilibili": ("bilibili_video", "create_time", ["title", "\"desc\""], None),
            "douyin": ("douyin_aweme", "create_time", ["title", "\"desc\""], None),
            "weibo": ("weibo_note", "create_time", ["content"], None),
            "zhihu": ("zhihu_content", "created_time", ["title", "content_text"], None),
            "tieba": ("tieba_note", "add_ts", ["title", "\"desc\""], None)
        }
        
        results = []
        images_results = []
        try:
            tasks = []
            platform_order = []
            for platform, (tb_name, col_time, cols, extra_cond) in table_config.items():
                cond_sql, params = self._build_keyword_conditions(query, cols)
                params.update(time_params)
                params["limit"] = limit
                
                if extra_cond:
                    sql = f"SELECT title, {cols[-1]} as content, {col_time} as time, {'url' if platform == 'seed' else 'note_url' if platform in ('xhs', 'tieba', 'weibo') else 'video_url' if platform in ('bilibili', 'douyin') else 'url'} as url, extra_info FROM {tb_name} WHERE {extra_cond} AND ({cond_sql}){time_filter.format(time_col=col_time)} ORDER BY {col_time} DESC LIMIT :limit"
                else:
                    # Weibo has no title column
                    if platform == "weibo":
                        sql = f"SELECT '' as title, content, {col_time} as time, note_url as url, extra_info FROM {tb_name} WHERE ({cond_sql}){time_filter.format(time_col=col_time)} ORDER BY {col_time} DESC LIMIT :limit"
                    else:
                        url_col = 'url' if platform in ('zhihu',) else 'note_url' if platform in ('xhs', 'tieba') else 'video_url' if platform in ('bilibili', 'douyin') else 'url'
                        sql = f"SELECT title, {cols[-1]} as content, {col_time} as time, {url_col} as url, extra_info FROM {tb_name} WHERE ({cond_sql}){time_filter.format(time_col=col_time)} ORDER BY {col_time} DESC LIMIT :limit"
                
                tasks.append(fetch_all(sql, params))
                platform_order.append(platform)
                
            all_rows = await asyncio.gather(*tasks, return_exceptions=True)
            for i, rows in enumerate(all_rows):
                if isinstance(rows, Exception):
                    logger.debug(f"查询本地库出错或表不存在: {rows}")
                    continue
                platform = platform_order[i]
                for r in rows:
                    title = r.get('title', '')
                    content = r.get('content', '')
                    time_val = r.get('time')
                    url = r.get('url')
                    extra_info_str = r.get('extra_info', '')
                    
                    raw_content = content
                    if extra_info_str:
                        try:
                            import json
                            extra_data = json.loads(extra_info_str)
                            if 'images' in extra_data and isinstance(extra_data['images'], list):
                                for img_url in extra_data['images']:
                                    if img_url:
                                        images_results.append(ImageResult(
                                            description=f"[{platform.upper()}] 图片",
                                            url=img_url
                                        ))
                            if 'video_url' in extra_data and extra_data['video_url']:
                                raw_content += f"\n[视频链接: {extra_data['video_url']}]"
                            
                            # 将完整 JSON 存入 raw_content 以供深度分析
                            raw_content += f"\n[附加数据: {json.dumps(extra_data, ensure_ascii=False)}]"
                        except Exception:
                            pass
                    
                    results.append(SearchResult(
                        title=f"[{platform.upper()}] {title}" if title else f"[{platform.upper()}] 网友讨论",
                        url=url or f"local://{platform}/{time_val}",
                        content=content[:300] + "..." if len(content) > 300 else content,
                        raw_content=raw_content,
                        published_date=self._parse_timestamp(time_val)
                    ))
        finally:
            import InsightEngine.utils.db as db_utils
            db_utils._engine = None  # Clear global engine to prevent event loop issues
        
        # 按照时间降序排序，并截取前 limit 个
        results.sort(key=lambda x: x.published_date or "", reverse=True)
        return results[:limit], images_results

    def _search_local_db(self, query: str, limit: int = 10, start_ts: int = 0, end_ts: int = 0):
        return _run_async(self._search_local_db_async(query, limit, start_ts, end_ts))

    @with_graceful_retry(SEARCH_API_RETRY_CONFIG, default_return=TavilyResponse(query="搜索失败"))
    def _search_internal(self, query: str, max_results: int = 10, start_ts: int = 0, end_ts: int = 0) -> TavilyResponse:
        """内部通用的搜索执行器，所有工具最终都调用此方法"""
        try:
            results, images = self._search_local_db(query, limit=max_results, start_ts=start_ts, end_ts=end_ts)
            return TavilyResponse(
                query=query, 
                answer=None,
                results=results, 
                images=images,
                response_time=0.1
            )
        except Exception as e:
            print(f"搜索时发生错误: {str(e)}")
            raise e

    # --- Agent 可用的工具方法 ---

    def basic_search_news(self, query: str, max_results: int = 7) -> TavilyResponse:
        """
        【工具】基础新闻搜索: 执行一次标准、快速的新闻搜索。
        """
        print(f"--- TOOL: 基础新闻搜索 (query: {query}) ---")
        return self._search_internal(query=query, max_results=max_results)

    def deep_search_news(self, query: str) -> TavilyResponse:
        """
        【工具】深度新闻分析: 对一个主题进行最全面、最深入的搜索。
        """
        print(f"--- TOOL: 深度新闻分析 (query: {query}) ---")
        return self._search_internal(query=query, max_results=20)

    def search_news_last_24_hours(self, query: str) -> TavilyResponse:
        """
        【工具】搜索24小时内新闻: 获取关于某个主题的最新动态。
        """
        print(f"--- TOOL: 搜索24小时内新闻 (query: {query}) ---")
        start_ts = int((datetime.datetime.now() - datetime.timedelta(days=1)).timestamp() * 1000)
        return self._search_internal(query=query, max_results=10, start_ts=start_ts)

    def search_news_last_week(self, query: str) -> TavilyResponse:
        """
        【工具】搜索本周新闻: 获取关于某个主题过去一周内的主要新闻报道。
        """
        print(f"--- TOOL: 搜索本周新闻 (query: {query}) ---")
        start_ts = int((datetime.datetime.now() - datetime.timedelta(weeks=1)).timestamp() * 1000)
        return self._search_internal(query=query, max_results=10, start_ts=start_ts)

    def search_images_for_news(self, query: str) -> TavilyResponse:
        """
        【工具】查找新闻图片: 搜索与某个新闻主题相关的图片。
        """
        print(f"--- TOOL: 查找新闻图片 (query: {query}) ---")
        return self._search_internal(query=query, max_results=5)

    def search_news_by_date(self, query: str, start_date: str, end_date: str) -> TavilyResponse:
        """
        【工具】按指定日期范围搜索新闻。
        """
        print(f"--- TOOL: 按指定日期范围搜索新闻 (query: {query}, from: {start_date}, to: {end_date}) ---")
        try:
            start_ts = int(datetime.datetime.strptime(start_date, '%Y-%m-%d').timestamp() * 1000)
            end_ts = int(datetime.datetime.strptime(end_date, '%Y-%m-%d').timestamp() * 1000)
        except Exception:
            start_ts = 0
            end_ts = 0
        return self._search_internal(query=query, max_results=15, start_ts=start_ts, end_ts=end_ts)


# --- 3. 测试与使用示例 ---

def print_response_summary(response: TavilyResponse):
    """简化的打印函数，用于展示测试结果，现在会显示发布日期"""
    if not response or not response.query:
        print("未能获取有效响应。")
        return
        
    print(f"\n查询: '{response.query}' | 耗时: {response.response_time}s")
    if response.answer:
        print(f"AI摘要: {response.answer[:120]}...")
    print(f"找到 {len(response.results)} 条网页, {len(response.images)} 张图片。")
    if response.results:
        first_result = response.results[0]
        date_info = f"(发布于: {first_result.published_date})" if first_result.published_date else ""
        print(f"第一条结果: {first_result.title} {date_info}")
    print("-" * 60)


if __name__ == "__main__":
    # 在运行前，请确保您已设置 TAVILY_API_KEY 环境变量
    
    try:
        # 初始化“新闻社”客户端，它内部包含了所有工具
        agency = TavilyNewsAgency()

        # 场景1: Agent 进行一次常规、快速的搜索
        response1 = agency.basic_search_news(query="奥运会最新赛况", max_results=5)
        print_response_summary(response1)

        # 场景2: Agent 需要全面了解“全球芯片技术竞争”的背景
        response2 = agency.deep_search_news(query="全球芯片技术竞争")
        print_response_summary(response2)

        # 场景3: Agent 需要追踪“GTC大会”的最新消息
        response3 = agency.search_news_last_24_hours(query="Nvidia GTC大会 最新发布")
        print_response_summary(response3)
        
        # 场景4: Agent 需要为一篇关于“自动驾驶”的周报查找素材
        response4 = agency.search_news_last_week(query="自动驾驶商业化落地")
        print_response_summary(response4)
        
        # 场景5: Agent 需要查找“韦伯太空望远镜”的新闻图片
        response5 = agency.search_images_for_news(query="韦伯太空望远镜最新发现")
        print_response_summary(response5)

        # 场景6: Agent 需要研究2025年第一季度关于“人工智能法规”的新闻
        response6 = agency.search_news_by_date(
            query="人工智能法规",
            start_date="2025-01-01",
            end_date="2025-03-31"
        )
        print_response_summary(response6)

    except ValueError as e:
        print(f"初始化失败: {e}")
        print("请确保 TAVILY_API_KEY 环境变量已正确设置。")
    except Exception as e:
        print(f"测试过程中发生未知错误: {e}")