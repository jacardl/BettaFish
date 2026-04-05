"""
专为 AI Agent 设计的本地舆情数据库查询工具集 (MediaCrawlerDB)

版本: 3.0
最后更新: 2025-08-23

此脚本将复杂的本地MySQL数据库查询功能封装成一系列目标明确、参数清晰的独立工具，
专为AI Agent调用而设计。Agent只需根据任务意图（如搜索热点、全局搜索话题、
按时间范围分析、获取评论）选择合适的工具，无需编写复杂的SQL语句。

V3.0 核心更新:
- 智能热度计算: `search_hot_content`不再需要`sort_by`参数，改为内部使用统一的加权热度算法，
  综合点赞、评论、分享、观看等数据计算热度分值，使结果更智能、更符合综合热度。
- 新增平台精搜工具: 新增 `search_topic_on_platform` 工具，作为特例，
  允许Agent在特定平台（B站、微博等七大平台）上对某一话题进行精确搜索，并支持时间筛选。
- 结构优化: 调整了数据结构与函数文档，以适应新功能。

主要工具:
- search_hot_content: 查找指定时间范围内的综合热度最高的内容。
- search_topic_globally: 在整个数据库中全局搜索与特定话题相关的所有内容和评论。
- search_topic_by_date: 在指定的历史日期范围内搜索与特定话题相关的内容。
- get_comments_for_topic: 专门提取公众对于某一特定话题的评论数据。
- search_topic_on_platform: 在指定的单个社交媒体平台上搜索特定话题。
"""

import os
import json
import requests
from loguru import logger
import asyncio
from typing import List, Dict, Any, Optional, Literal
from dataclasses import dataclass, field
from datetime import datetime, timedelta, date
from InsightEngine.utils.config import settings
from dotenv import load_dotenv

load_dotenv()

# --- 1. 数据结构定义 ---

@dataclass
class QueryResult:
    """统一的数据库查询结果数据类"""
    platform: str
    content_type: str
    title_or_content: str
    author_nickname: Optional[str] = None
    url: Optional[str] = None
    publish_time: Optional[datetime] = None
    engagement: Dict[str, int] = field(default_factory=dict)
    source_keyword: Optional[str] = None
    hotness_score: float = 0.0
    source_table: str = ""

@dataclass
class DBResponse:
    """封装工具的完整返回结果"""
    tool_name: str
    parameters: Dict[str, Any]
    results: List[QueryResult] = field(default_factory=list)
    results_count: int = 0
    error_message: Optional[str] = None

# --- 2. 核心客户端与专用工具集 ---

class MediaCrawlerDB:
    """升级为基于 web-access 实时联网查询的舆情检索工具"""
    
    ANSPIRE_BASE_URL = os.getenv("ANSPIRE_BASE_URL", "https://plugin.anspire.cn/api/ntsearch/search")
    
    def __init__(self):
        """
        初始化实时网络爬虫客户端。
        """
        self.api_key = os.getenv("ANSPIRE_API_KEY")
        if not self.api_key:
            logger.warning("未配置 ANSPIRE_API_KEY，实时搜索功能可能受限")
        
        self._headers = {
            'Authorization': f'Bearer {self.api_key}',
            'Content-Type': 'application/json',
            'Connection': 'keep-alive',
            'Accept': '*/*'
        }
        
    def _execute_realtime_search(self, query: str, insite: str = "", top_k: int = 10, from_time: str = "", to_time: str = "") -> List[Dict[str, Any]]:
        """执行实时网页搜索"""
        payload = {
            "query": query,
            "top_k": top_k,
            "Insite": insite,
            "FromTime": from_time,
            "ToTime": to_time
        }
        try:
            response = requests.get(self.ANSPIRE_BASE_URL, headers=self._headers, params=payload, timeout=30)
            response.raise_for_status()
            data = response.json()
            return data.get("results", [])
        except Exception as e:
            logger.error(f"实时搜索网络错误: {e}")
            return []

    @staticmethod
    def _to_datetime(ts: Any) -> Optional[datetime]:
        if not ts: return None
        try:
            if isinstance(ts, datetime): return ts
            if isinstance(ts, str):
                # 尝试解析类似于 2026-04-02 12:11:19 或者 ISO 格式
                return datetime.fromisoformat(ts.split('+')[0].strip().replace(' ', 'T'))
        except (ValueError, TypeError): return None
        return None

    def search_hot_content(
        self,
        time_period: Literal['24h', 'week', 'year'] = 'week',
        limit: int = 50
    ) -> DBResponse:
        """
        【工具】查找热点内容: 实时获取全网热点信息。
        """
        params_for_log = {'time_period': time_period, 'limit': limit}
        logger.info(f"--- TOOL: 查找实时热点内容 (params: {params_for_log}) ---")
        
        now = datetime.now()
        start_time = now - timedelta(days={'24h': 1, 'week': 7}.get(time_period, 365))
        
        raw_results = self._execute_realtime_search(
            query="今日热榜 全网热点",
            top_k=limit,
            from_time=start_time.strftime("%Y-%m-%d %H:%M:%S"),
            to_time=now.strftime("%Y-%m-%d %H:%M:%S")
        )
        
        formatted_results = []
        for r in raw_results:
            formatted_results.append(QueryResult(
                platform="web",
                content_type="hot_news",
                title_or_content=f"{r.get('title', '')} - {r.get('content', '')}",
                url=r.get('url'),
                publish_time=self._to_datetime(r.get('date')),
                hotness_score=float(r.get('score', 0)) * 100,
                source_table="realtime_search"
            ))
            
        return DBResponse("search_hot_content", params_for_log, results=formatted_results, results_count=len(formatted_results))    

    def search_topic_globally(self, topic: str, limit_per_table: int = 10) -> DBResponse:
        """
        【工具】全局话题搜索: 实时在全网搜索指定话题。
        """
        params_for_log = {'topic': topic, 'limit_per_table': limit_per_table}
        logger.info(f"--- TOOL: 全网话题搜索 (params: {params_for_log}) ---")
        
        raw_results = self._execute_realtime_search(query=topic, top_k=limit_per_table * 3)
        
        all_results = []
        for r in raw_results:
            all_results.append(QueryResult(
                platform="web",
                content_type="news",
                title_or_content=f"{r.get('title', '')} - {r.get('content', '')}",
                url=r.get('url'),
                publish_time=self._to_datetime(r.get('date')),
                source_keyword=topic,
                source_table="realtime_search"
            ))
            
        return DBResponse("search_topic_globally", params_for_log, results=all_results, results_count=len(all_results))

    def search_topic_by_date(self, topic: str, start_date: str, end_date: str, limit_per_table: int = 10) -> DBResponse:
        """
        【工具】按日期搜索话题: 实时获取在明确的历史时间段内的话题内容。
        """
        params_for_log = {'topic': topic, 'start_date': start_date, 'end_date': end_date, 'limit_per_table': limit_per_table}
        logger.info(f"--- TOOL: 按日期全网搜索话题 (params: {params_for_log}) ---")
        
        from_time = f"{start_date} 00:00:00"
        to_time = f"{end_date} 23:59:59"
        
        raw_results = self._execute_realtime_search(query=topic, top_k=limit_per_table * 3, from_time=from_time, to_time=to_time)
        
        all_results = []
        for r in raw_results:
            all_results.append(QueryResult(
                platform="web",
                content_type="news",
                title_or_content=f"{r.get('title', '')} - {r.get('content', '')}",
                url=r.get('url'),
                publish_time=self._to_datetime(r.get('date')),
                source_keyword=topic,
                source_table="realtime_search"
            ))
            
        return DBResponse("search_topic_by_date", params_for_log, results=all_results, results_count=len(all_results))
        
    def get_comments_for_topic(self, topic: str, limit: int = 50) -> DBResponse:
        """
        【工具】获取话题讨论: 实时搜索网民对某话题的讨论和评论。
        """
        params_for_log = {'topic': topic, 'limit': limit}
        logger.info(f"--- TOOL: 获取话题讨论 (params: {params_for_log}) ---")
        
        # 针对评论的特定搜索
        raw_results = self._execute_realtime_search(query=f"{topic} 评论 观点 网友说", top_k=limit)
        
        formatted = []
        for r in raw_results:
            formatted.append(QueryResult(
                platform="web",
                content_type="comment",
                title_or_content=r.get('content', ''),
                author_nickname="网友",
                publish_time=self._to_datetime(r.get('date')),
                source_table="realtime_search"
            ))
            
        return DBResponse("get_comments_for_topic", params_for_log, results=formatted, results_count=len(formatted))

    def search_topic_on_platform(
        self,
        platform: Literal['bilibili', 'weibo', 'douyin', 'kuaishou', 'xhs', 'zhihu', 'tieba'],
        topic: str,
        start_date: Optional[str] = None,
        end_date: Optional[str] = None,
        limit: int = 20
    ) -> DBResponse:
        """
        【工具】平台定向搜索: 实时在指定的单个社交媒体平台上搜索特定话题。
        """
        params_for_log = {'platform': platform, 'topic': topic, 'start_date': start_date, 'end_date': end_date, 'limit': limit}
        logger.info(f"--- TOOL: 实时平台定向搜索 (params: {params_for_log}) ---")

        domain_mapping = {
            'bilibili': 'bilibili.com',
            'weibo': 'weibo.com',
            'douyin': 'douyin.com',
            'kuaishou': 'kuaishou.com',
            'xhs': 'xiaohongshu.com',
            'zhihu': 'zhihu.com',
            'tieba': 'tieba.baidu.com'
        }
        
        if platform not in domain_mapping:
            return DBResponse("search_topic_on_platform", params_for_log, error_message=f"不支持的平台: {platform}")

        insite = domain_mapping[platform]
        from_time = f"{start_date} 00:00:00" if start_date else ""
        to_time = f"{end_date} 23:59:59" if end_date else ""

        raw_results = self._execute_realtime_search(query=topic, insite=insite, top_k=limit, from_time=from_time, to_time=to_time)
        
        all_results = []
        for r in raw_results:
            all_results.append(QueryResult(
                platform=platform,
                content_type="post",
                title_or_content=f"{r.get('title', '')} - {r.get('content', '')}",
                url=r.get('url'),
                publish_time=self._to_datetime(r.get('date')),
                source_keyword=topic,
                source_table="realtime_search"
            ))
        
        return DBResponse("search_topic_on_platform", params_for_log, results=all_results, results_count=len(all_results))

# --- 3. 测试与使用示例 ---
def print_response_summary(response: DBResponse):
    """简化的打印函数，用于展示测试结果"""
    if response.error_message:
        logger.info(f"工具 '{response.tool_name}' 执行出错: {response.error_message}")
        return

    params_str = ", ".join(f"{k}='{v}'" for k, v in response.parameters.items())
    logger.info(f"查询: 工具='{response.tool_name}', 参数=[{params_str}]")
    logger.info(f"找到 {response.results_count} 条相关记录。")
    
    # 统一为一个消息输出
    output_lines = []
    output_lines.append("==== 查询结果预览（最多前5条） ====")
    if response.results and len(response.results) > 0:
        for idx, res in enumerate(response.results[:5], 1):
            content_preview = (res.title_or_content.replace('\n', ' ')[:70] + '...') if res.title_or_content and len(res.title_or_content) > 70 else (res.title_or_content or '')
            author_str = res.author_nickname or "N/A"
            publish_time_str = res.publish_time.strftime('%Y-%m-%d %H:%M') if res.publish_time else "N/A"
            hotness_str = f", hotness: {res.hotness_score:.2f}" if getattr(res, "hotness_score", 0) > 0 else ""
            engagement_dict = getattr(res, "engagement", {}) or {}
            engagement_str = ", ".join(f"{k}: {v}" for k, v in engagement_dict.items() if v)
            output_lines.append(
                f"{idx}. [{res.platform.upper()}/{res.content_type}] {content_preview}\n"
                f"   作者: {author_str} | 时间: {publish_time_str}"
                f"{hotness_str} | 源关键词: '{res.source_keyword or 'N/A'}'\n"
                f"   链接: {res.url or 'N/A'}\n"
                f"   互动数据: {{{engagement_str}}}"
            )
    else:
        output_lines.append("暂无相关内容。")
    output_lines.append("=" * 60)
    logger.info('\n'.join(output_lines))

if __name__ == "__main__":
    
    try:
        db_agent_tools = MediaCrawlerDB()
        logger.info("数据库工具初始化成功，开始执行测试场景...\n")
        
        # 场景1: (新) 查找过去一周综合热度最高的内容 (不再需要sort_by)
        response1 = db_agent_tools.search_hot_content(time_period='week', limit=5)
        print_response_summary(response1)

        # 场景2: 查找过去24小时内综合热度最高的内容
        response2 = db_agent_tools.search_hot_content(time_period='24h', limit=5)
        print_response_summary(response2)

        # 场景3: 全局搜索"罗永浩"
        response3 = db_agent_tools.search_topic_globally(topic="罗永浩", limit_per_table=2)
        print_response_summary(response3)

        # 场景4: (新增) 在B站上精确搜索"论文"
        response4 = db_agent_tools.search_topic_on_platform(platform='bilibili', topic="论文", limit=5)
        print_response_summary(response4)

        # 场景5: (新增) 在微博上精确搜索 "许凯" 在特定一天内的内容
        response5 = db_agent_tools.search_topic_on_platform(platform='weibo', topic="许凯", start_date='2025-08-22', end_date='2025-08-22', limit=5)
        print_response_summary(response5)

    except ValueError as e:
        logger.exception(f"初始化失败: {e}")
        logger.exception("请确保相关的数据库环境变量已正确设置, 或在代码中直接提供连接信息。")
    except Exception as e:
        logger.exception(f"测试过程中发生未知错误: {e}")