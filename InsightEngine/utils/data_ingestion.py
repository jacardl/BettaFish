import os
import json
import asyncio
import requests
from datetime import datetime
from pathlib import Path
from loguru import logger
from InsightEngine.utils.db import execute_write, fetch_all
from InsightEngine.utils.config import settings

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

def ingest_seed_data(seed_id: str):
    """将用户上传的 seed 文件内容解析并持久化到本地数据库中"""
    root_dir = Path(__file__).parent.parent.parent
    seed_path_json = root_dir / 'output' / 'seeds' / f"{seed_id}.json"
    seed_path_txt = root_dir / 'output' / 'seeds' / f"{seed_id}.txt"
    
    seed_text = ""
    title = "用户上传的参考资料(Seed)"
    url = f"seed://{seed_id}/attachment"
    
    if seed_path_json.exists():
        try:
            data = json.loads(seed_path_json.read_text(encoding='utf-8'))
            seed_text = data.get('text', '')
            title = data.get('filename', title)
            url = data.get('fake_url', url)
        except Exception as e:
            logger.error(f"解析 Seed JSON 失败: {e}")
    elif seed_path_txt.exists():
        seed_text = seed_path_txt.read_text(encoding='utf-8')
        
    if not seed_text:
        return
        
    now_ts = int(datetime.now().timestamp() * 1000)
    news_id = f"seed_{seed_id[:8]}_{now_ts}"
    crawl_date = datetime.now().date()
    
    sql = """
        INSERT INTO daily_news (news_id, source_platform, title, description, url, crawl_date, add_ts, last_modify_ts, rank_position)
        VALUES (:nid, :platform, :title, :desc, :url, :cdate, :add_ts, :add_ts, 1)
    """
    params = {
        "nid": news_id,
        "platform": "seed_document",
        "title": title,
        "desc": seed_text[:20000],  # 截断超长文本防止爆库
        "url": url,
        "cdate": crawl_date,
        "add_ts": now_ts
    }
    try:
        affected = _run_async(execute_write(sql, params))
        logger.info(f"✅ Seed 文件 ({seed_id}) 已持久化至 daily_news 表, 影响行数: {affected}")
    except Exception as e:
        logger.error(f"❌ 插入 Seed 数据失败: {e}")
    finally:
        import InsightEngine.utils.db as db_utils
        db_utils._engine = None  # Prevent event loop reuse issues


def ingest_incremental_anspire_data(query: str):
    """通过 Anspire API 实时抓取增量数据，并插入到本地对应的数据表中"""
    if not query:
        return
        
    anspire_key = os.getenv("ANSPIRE_API_KEY") or getattr(settings, "ANSPIRE_API_KEY", None)
    if not anspire_key:
        logger.warning("未配置 ANSPIRE_API_KEY，无法执行增量抓取")
        return
        
    use_pro = str(os.getenv("ANSPIRE_USE_PRO", "True")).lower() in ("true", "1", "yes", "t")
    target_url = getattr(settings, "ANSPIRE_PRO_BASE_URL", "https://plugin.anspire.cn/api/ntsearch/prosearch") if use_pro else getattr(settings, "ANSPIRE_BASE_URL", "https://plugin.anspire.cn/api/ntsearch/search")
    
    headers = {
        'Authorization': f'Bearer {anspire_key}',
        'Content-Type': 'application/json'
    }
    
    payload = {
        "query": query,
        "top_k": 20, # 只抓取最相关的 20 条增量
        "detail": True if use_pro else False
    }
    
    logger.info(f"🔄 正在从 Anspire 获取 '{query}' 的增量数据...")
    try:
        response = requests.get(target_url, headers=headers, params=payload, timeout=30)
        response.raise_for_status()
        results = response.json().get("results", [])
    except Exception as e:
        logger.error(f"❌ 增量抓取请求失败: {e}")
        return
        
    if not results:
        logger.info("未抓取到任何增量数据")
        return
        
    now_ts = int(datetime.now().timestamp() * 1000)
    inserted_count = 0
    
    # 简单的平台推断映射
    for r in results:
        url = r.get("url", "")
        title = r.get("title", "")
        content = r.get("content", "")
        date_str = r.get("date")
        
        # 解析时间戳
        ts = now_ts
        if date_str:
            try:
                dt = datetime.fromisoformat(date_str.split('+')[0].strip().replace(' ', 'T'))
                ts = int(dt.timestamp() * 1000)
            except Exception:
                pass
                
        # 判断路由
        sql = ""
        params = {}
        if "bilibili.com" in url:
            import hashlib
            video_id = "anspire_" + hashlib.md5(url.encode()).hexdigest()[:16]
            sql = "INSERT INTO bilibili_video (title, \"desc\", video_id, video_url, create_time, nickname) VALUES (:t, :d, :vid, :u, :ts, :a)"
            params = {"t": title, "d": content, "vid": video_id, "u": url, "ts": ts, "a": "B站用户"}
        elif "xiaohongshu.com" in url:
            sql = "INSERT INTO xhs_note (title, \"desc\", note_url, time, nickname) VALUES (:t, :d, :u, :ts, :a)"
            params = {"t": title, "d": content, "u": url, "ts": ts, "a": "小红书用户"}
        elif "douyin.com" in url:
            sql = "INSERT INTO douyin_aweme (title, \"desc\", aweme_url, create_time, nickname) VALUES (:t, :d, :u, :ts, :a)"
            params = {"t": title, "d": content, "u": url, "ts": ts, "a": "抖音用户"}
        elif "weibo.com" in url:
            sql = "INSERT INTO weibo_note (content, note_url, create_time, nickname) VALUES (:d, :u, :ts, :a)"
            params = {"d": content, "u": url, "ts": ts, "a": "微博用户"}
        else:
            import hashlib
            news_id = "anspire_" + hashlib.md5(url.encode()).hexdigest()[:16]
            crawl_date = datetime.now().date()
            # 其他全部入每日热点表，标记平台为 web
            sql = "INSERT INTO daily_news (news_id, source_platform, title, description, url, crawl_date, add_ts, last_modify_ts, rank_position) VALUES (:nid, 'web', :t, :d, :u, :cdate, :ts, :ts, 99)"
            params = {"nid": news_id, "t": title, "d": content, "u": url, "cdate": crawl_date, "ts": ts}
            
        try:
            _run_async(execute_write(sql, params))
            inserted_count += 1
        except Exception as e:
            # 表可能不存在或主键冲突，静默忽略
            logger.debug(f"插入数据失败: {e}")
            pass
        finally:
            import InsightEngine.utils.db as db_utils
            db_utils._engine = None  # Prevent event loop reuse issues
            
    logger.info(f"✅ 增量数据抓取完成: 成功向本地数据库插入 {inserted_count} 条最新记录")
