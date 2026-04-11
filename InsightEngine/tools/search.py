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
from typing import List, Dict, Any, Optional, Literal, Tuple
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
import concurrent.futures
from InsightEngine.utils.db import fetch_all

def _run_async(coro):
    """安全的异步执行包装器，兼容多线程与事件循环环境"""
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None
        
    if loop and loop.is_running():
        with concurrent.futures.ThreadPoolExecutor(1) as pool:
            return pool.submit(asyncio.run, coro).result()
    else:
        return asyncio.run(coro)

class MediaCrawlerDB:
    """本地数据库舆情检索工具"""
    
    def __init__(self):
        """初始化，无需外部API"""
        pass

    @staticmethod
    def _parse_timestamp(ts: Any) -> Optional[datetime]:
        if not ts: return None
        try:
            if isinstance(ts, datetime):
                return ts
            if isinstance(ts, str) and ts.isdigit():
                ts = int(ts)
            if isinstance(ts, int):
                if ts > 1e11:  # 13位毫秒时间戳
                    return datetime.fromtimestamp(ts / 1000)
                return datetime.fromtimestamp(ts)
            if isinstance(ts, str):
                return datetime.fromisoformat(ts.split('+')[0].strip().replace(' ', 'T'))
        except Exception:
            return None
        return None
        
    def _safe_query(self, sql: str, params: dict) -> List[dict]:
        """安全查询单表，如果表不存在则静默忽略"""
        try:
            res = _run_async(fetch_all(sql, params))
            import InsightEngine.utils.db as db_utils
            db_utils._engine = None  # Prevent event loop reuse issues
            return res
        except Exception as e:
            # 捕获表不存在等异常，不影响其他表查询
            logger.debug(f"查询跳过 (可能表不存在): {e}")
            import InsightEngine.utils.db as db_utils
            db_utils._engine = None
            return []

    def _build_keyword_conditions(self, topic: str, columns: List[str]) -> Tuple[str, dict]:
        """
        (不再使用纯 LIKE 模糊匹配，保留此函数以防其他地方调用)
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
        
    def _build_vector_conditions(self, topic: str) -> Tuple[str, dict]:
        """
        使用本地 Embedding 进行向量相似度匹配。
        返回: SQL 排序/计算片段 和 params (包含 query_vector 字符串)
        """
        from utils.embedding import get_embedding
        try:
            emb = get_embedding(topic)
            emb_str = f"[{','.join(map(str, emb))}]"
            # Postgres pgvector: `<=>` represents cosine distance. We want cosine similarity, which is 1 - distance.
            # But for ORDER BY, distance ASC is exactly what we want (smaller distance = more similar).
            # If embedding is null, distance will be null.
            order_sql = "embedding <=> :query_vector"
            return order_sql, {"query_vector": emb_str}
        except Exception as e:
            logger.error(f"Generate embedding for query failed: {e}")
            return "1", {} # fallback

    def search_hot_content(
        self,
        time_period: Literal['24h', 'week', 'year'] = 'week',
        limit: int = 50
    ) -> DBResponse:
        """
        【工具】查找热点内容: 提取数据库中 MindSpider 抓取的每日热点。
        """
        params_for_log = {'time_period': time_period, 'limit': limit}
        logger.info(f"--- TOOL: 查找本地热点内容 (params: {params_for_log}) ---")
        
        now = datetime.now()
        days = {'24h': 1, 'week': 7, 'year': 365}.get(time_period, 7)
        start_ts = int((now - timedelta(days=days)).timestamp() * 1000)
        
        sql = """
            SELECT source_platform as platform, title, description as content, url, add_ts as publish_time
            FROM daily_news
            WHERE add_ts >= :start_ts
            ORDER BY rank_position ASC, add_ts DESC
            LIMIT :limit
        """
        raw_results = self._safe_query(sql, {"start_ts": start_ts, "limit": limit})
        
        formatted = []
        for r in raw_results:
            formatted.append(QueryResult(
                platform=r.get('platform', 'web'),
                content_type="hot_news",
                title_or_content=f"{r.get('title', '')} - {r.get('content', '')}",
                url=r.get('url'),
                publish_time=self._parse_timestamp(r.get('publish_time')),
                source_table="daily_news"
            ))
            
        return DBResponse("search_hot_content", params_for_log, results=formatted, results_count=len(formatted))

    def search_topic_globally(self, topic: str, limit_per_table: int = 10) -> DBResponse:
        """
        【工具】全局话题搜索: 在本地多平台数据表中模糊检索话题。
        """
        params_for_log = {'topic': topic, 'limit_per_table': limit_per_table}
        logger.info(f"--- TOOL: 本地全库话题搜索 (params: {params_for_log}) ---")
        
        table_config = {
            "seed": ("daily_news", "add_ts", ["title", "description"], "source_platform = 'seed_document'"),
            "xhs": ("xhs_note", "time", ["title", "\"desc\""], None),
            "bilibili": ("bilibili_video", "create_time", ["title", "\"desc\""], None),
            "douyin": ("douyin_aweme", "create_time", ["title", "\"desc\""], None),
            "weibo": ("weibo_note", "create_time", ["content"], None),
            "zhihu": ("zhihu_content", "created_time", ["title", "content_text"], None),
            "kuaishou": ("kuaishou_video", "create_time", ["title", "\"desc\""], None),
            "tieba": ("tieba_note", "add_ts", ["title", "\"desc\""], None)
        }
        
        all_results = []
        for platform, (tb_name, col_time, cols, extra_cond) in table_config.items():
            order_sql, params = self._build_vector_conditions(topic)
            params["limit"] = limit_per_table
            
            # 使用 embedding IS NOT NULL 来确保我们只搜索已向量化的数据，或者你可以退回到 LIKE
            if "query_vector" in params:
                cond_sql = f"embedding IS NOT NULL"
                order_clause = f"{order_sql} ASC"
            else:
                # 降级处理
                cond_sql, fallback_params = self._build_keyword_conditions(topic, cols)
                params.update(fallback_params)
                order_clause = f"{col_time} DESC"
            
            if extra_cond:
                sql = f"SELECT * FROM {tb_name} WHERE {extra_cond} AND ({cond_sql}) ORDER BY {order_clause} LIMIT :limit"
            else:
                sql = f"SELECT * FROM {tb_name} WHERE {cond_sql} ORDER BY {order_clause} LIMIT :limit"
                
            rows = self._safe_query(sql, params)
            for r in rows:
                title = r.get('title', '')
                content = r.get('desc', '') or r.get('content_text', '') or r.get('description', '') or r.get('content', '')
                time_val = r.get('time') or r.get('created_time') or r.get('add_ts') or r.get('create_time')
                url = r.get('note_url') or r.get('video_url') or r.get('content_url') or r.get('aweme_url') or r.get('url')
                
                # 提取额外格式数据
                extra_info_str = r.get('extra_info', '')
                if extra_info_str:
                    try:
                        import json
                        extra_data = json.loads(extra_info_str)
                        if 'images' in extra_data and extra_data['images']:
                            content += f" [包含 {len(extra_data['images'])} 张图片]"
                        if 'video_url' in extra_data and extra_data['video_url']:
                            content += f" [包含视频]"
                        # 可以提取更多的点赞等互动数据
                    except Exception:
                        pass
                
                all_results.append(QueryResult(
                    platform=platform,
                    content_type="news",
                    title_or_content=f"{title} - {content}",
                    url=url,
                    publish_time=self._parse_timestamp(time_val),
                    source_keyword=topic,
                    source_table=f"{platform}_table"
                ))
                
        return DBResponse("search_topic_globally", params_for_log, results=all_results, results_count=len(all_results))

    def search_topic_by_date(self, topic: str, start_date: str, end_date: str, limit_per_table: int = 10) -> DBResponse:
        """
        【工具】按日期搜索话题: 在限定的时间段内查询本地库。
        """
        params_for_log = {'topic': topic, 'start_date': start_date, 'end_date': end_date, 'limit_per_table': limit_per_table}
        logger.info(f"--- TOOL: 本地按日期搜索话题 (params: {params_for_log}) ---")
        
        try:
            start_ts = int(datetime.strptime(start_date, "%Y-%m-%d").timestamp() * 1000)
            end_ts = int((datetime.strptime(end_date, "%Y-%m-%d") + timedelta(days=1)).timestamp() * 1000)
        except Exception:
            return DBResponse("search_topic_by_date", params_for_log, error_message="日期格式错误，需为 YYYY-MM-DD")
            
        table_config = {
            "xhs": ("xhs_note", "time", ["title", "\"desc\""]),
            "bilibili": ("bilibili_video", "create_time", ["title", "\"desc\""]),
            "douyin": ("douyin_aweme", "create_time", ["title", "\"desc\""]),
            "weibo": ("weibo_note", "create_time", ["content"]),
            "tieba": ("tieba_note", "add_ts", ["title", "\"desc\""])
        }
        
        all_results = []
        for platform, (tb_name, col_time, cols) in table_config.items():
            order_sql, params = self._build_vector_conditions(topic)
            params["start_ts"] = start_ts
            params["end_ts"] = end_ts
            params["limit"] = limit_per_table
            
            if "query_vector" in params:
                cond_sql = f"embedding IS NOT NULL"
                order_clause = f"{order_sql} ASC"
            else:
                cond_sql, fallback_params = self._build_keyword_conditions(topic, cols)
                params.update(fallback_params)
                order_clause = f"{col_time} DESC"
            
            sql = f"SELECT *, extra_info FROM {tb_name} WHERE ({cond_sql}) AND {col_time} >= :start_ts AND {col_time} <= :end_ts ORDER BY {order_clause} LIMIT :limit"
            
            rows = self._safe_query(sql, params)
            for r in rows:
                title = r.get('title', '')
                content = r.get('desc', '') or r.get('content_text', '')
                time_val = r.get('time') or r.get('created_time') or r.get('add_ts')
                
                # 提取额外格式数据
                extra_info_str = r.get('extra_info', '')
                if extra_info_str:
                    try:
                        import json
                        extra_data = json.loads(extra_info_str)
                        if 'images' in extra_data and extra_data['images']:
                            content += f" [包含 {len(extra_data['images'])} 张图片]"
                        if 'video_url' in extra_data and extra_data['video_url']:
                            content += f" [包含视频]"
                    except Exception:
                        pass

                all_results.append(QueryResult(
                    platform=platform,
                    content_type="news",
                    title_or_content=f"{title} - {content}",
                    url=r.get('note_url') or r.get('video_url') or r.get('content_url') or r.get('aweme_url') or r.get('url'),
                    publish_time=self._parse_timestamp(time_val),
                    source_keyword=topic,
                    source_table=f"{platform}_table"
                ))
                
        return DBResponse("search_topic_by_date", params_for_log, results=all_results, results_count=len(all_results))
        
    def get_comments_for_topic(self, topic: str, limit: int = 50) -> DBResponse:
        """
        【工具】获取话题讨论: 直接从本地各平台的评论表中挖掘网民原话。
        """
        params_for_log = {'topic': topic, 'limit': limit}
        logger.info(f"--- TOOL: 本地挖掘话题讨论 (params: {params_for_log}) ---")
        
        table_config = {
            "xhs": ("xhs_note_comment", "create_time"),
            "douyin": ("douyin_aweme_comment", "create_time"),
            "bilibili": ("bilibili_video_comment", "create_time"),
            "weibo": ("weibo_note_comment", "create_time"),
            "zhihu": ("zhihu_comment", "publish_time"),
            "tieba": ("tieba_comment", "publish_time"),
        }
        
        formatted = []
        for platform, (tb_name, col_time) in table_config.items():
            order_sql, params = self._build_vector_conditions(topic)
            params["limit"] = limit
            
            if "query_vector" in params:
                cond_sql = f"embedding IS NOT NULL"
                order_clause = f"{order_sql} ASC"
            else:
                cond_sql, fallback_params = self._build_keyword_conditions(topic, ["content"])
                params.update(fallback_params)
                order_clause = f"{col_time} DESC"
                
            sql = f"SELECT content, {col_time} as time, nickname FROM {tb_name} WHERE {cond_sql} ORDER BY {order_clause} LIMIT :limit"
            # 兼容知乎贴吧字段名
            if platform in ["zhihu", "tieba"]:
                sql = sql.replace("nickname", "user_nickname as nickname")
                
            rows = self._safe_query(sql, params)
            for r in rows:
                formatted.append(QueryResult(
                    platform=platform,
                    content_type="comment",
                    title_or_content=r.get('content', ''),
                    author_nickname=r.get('nickname', '网友'),
                    publish_time=self._parse_timestamp(r.get('time')),
                    source_table=f"{platform}_comment"
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
        【工具】平台定向搜索: 精确查询本地某单一平台的数据表。
        """
        params_for_log = {'platform': platform, 'topic': topic, 'start_date': start_date, 'end_date': end_date, 'limit': limit}
        logger.info(f"--- TOOL: 本地定向平台搜索 (params: {params_for_log}) ---")

        table_map = {
            'xhs': ('xhs_note', 'title', 'desc', 'time'),
            'bilibili': ('bilibili_video', 'title', 'desc', 'create_time'),
            'douyin': ('douyin_aweme', 'title', 'desc', 'create_time'),
            'weibo': ('weibo_note', 'content', 'content', 'create_time'), # 微博通常用content代替title和desc
            'kuaishou': ('kuaishou_video', 'title', 'desc', 'create_time'),
            'zhihu': ('zhihu_content', 'title', 'content_text', 'created_time'),
            'tieba': ('tieba_note', 'title', 'desc', 'add_ts')
        }
        
        if platform not in table_map:
            return DBResponse("search_topic_on_platform", params_for_log, error_message=f"不支持的平台: {platform}")

        tb_name, col_title, col_desc, col_time = table_map[platform]
        
        order_sql, params = self._build_vector_conditions(topic)
        params["limit"] = limit
        
        if "query_vector" in params:
            cond_sql = f"embedding IS NOT NULL"
            order_clause = f"{order_sql} ASC"
        else:
            cond_sql, fallback_params = self._build_keyword_conditions(topic, [col_title, f'"{col_desc}"'])
            params.update(fallback_params)
            order_clause = f"{col_time} DESC"
            
        # 构建时间过滤
        time_filter = ""
        if start_date and end_date:
            try:
                start_ts = int(datetime.strptime(start_date, "%Y-%m-%d").timestamp() * 1000)
                end_ts = int((datetime.strptime(end_date, "%Y-%m-%d") + timedelta(days=1)).timestamp() * 1000)
                time_filter = f" AND {col_time} >= :start_ts AND {col_time} <= :end_ts"
                params["start_ts"] = start_ts
                params["end_ts"] = end_ts
            except Exception:
                pass
                
        sql = f"SELECT * FROM {tb_name} WHERE ({cond_sql}) {time_filter} ORDER BY {order_clause} LIMIT :limit"
        
        rows = self._safe_query(sql, params)
        
        all_results = []
        for r in rows:
            title = r.get(col_title, '') if col_title != 'desc' else ''
            content = r.get(col_desc, '')
            
            all_results.append(QueryResult(
                platform=platform,
                content_type="news",
                title_or_content=f"{title} - {content}",
                url=r.get('note_url') or r.get('video_url') or r.get('content_url') or r.get('aweme_url') or r.get('url'),
                publish_time=self._parse_timestamp(r.get(col_time)),
                source_keyword=topic,
                source_table=tb_name
            ))
            
        return DBResponse("search_topic_on_platform", params_for_log, results=all_results, results_count=len(all_results))

def get_search_tools():
    """返回供LLM调用的工具列表"""
    search_db = MediaCrawlerDB()
    return [
        search_db.search_hot_content,
        search_db.search_topic_globally,
        search_db.search_topic_by_date,
        search_db.get_comments_for_topic,
        search_db.search_topic_on_platform
    ]

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