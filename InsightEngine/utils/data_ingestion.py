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
        
        # 将 seed 的入库行为记录到 crawler.log
        log_file = root_dir / "logs" / "crawler.log"
        if log_file.parent.exists():
            with open(log_file, "a", encoding="utf-8") as f:
                ts = datetime.now().strftime('%H:%M:%S')
                f.write(f"[{ts}] [SYSTEM] 📥 [Seed入库] 成功将用户上传的附件 '{title}' (ID: {seed_id}) 解析并写入本地 daily_news 数据库。\n")
                
    except Exception as e:
        logger.error(f"❌ 插入 Seed 数据失败: {e}")
    finally:
        import InsightEngine.utils.db as db_utils
        db_utils._engine = None  # Prevent event loop reuse issues


def ingest_incremental_anspire_data(query: str):
    """通过 Anspire API 实时抓取增量数据，并插入到本地对应的数据表中
    返回: (插入总行数: int, 分平台明细: str)
    """
    if not query:
        return 0, "查询为空"
        
    anspire_key = os.getenv("ANSPIRE_API_KEY") or getattr(settings, "ANSPIRE_API_KEY", None)
    if not anspire_key:
        logger.warning("未配置 ANSPIRE_API_KEY，无法执行增量抓取")
        return 0, "未配置 API Key"
        
    use_pro = str(os.getenv("ANSPIRE_USE_PRO", "True")).lower() in ("true", "1", "yes", "t")
    target_url = getattr(settings, "ANSPIRE_PRO_BASE_URL", "https://plugin.anspire.cn/api/ntsearch/prosearch") if use_pro else getattr(settings, "ANSPIRE_BASE_URL", "https://plugin.anspire.cn/api/ntsearch/search")
    
    headers = {
        'Authorization': f'Bearer {anspire_key}',
        'Content-Type': 'application/json'
    }
    
    payload = {
        "query": query,
        "top_k": 100, # 尽可能多地获取数据
        "detail": True # 强制获取详细信息（完整 raw data）
    }
    
    logger.info(f"🔄 正在从 Anspire 获取 '{query}' 的增量数据...")
    try:
        response = requests.get(target_url, headers=headers, params=payload, timeout=45)
        response.raise_for_status()
        results = response.json().get("results", [])
    except Exception as e:
        logger.error(f"❌ 增量抓取请求失败: {e}")
        return 0, f"请求失败: {e}"
        
    if not results:
        logger.info("未抓取到任何增量数据")
        return 0, "API 返回 0 条结果"
        
    now_ts = int(datetime.now().timestamp() * 1000)
    
    # 【新增机制：保存完整的 raw data】
    import json
    from pathlib import Path
    try:
        raw_dir = Path("logs/raw_data")
        raw_dir.mkdir(parents=True, exist_ok=True)
        raw_file = raw_dir / f"crawler_raw_{now_ts}.json"
        with open(raw_file, "w", encoding="utf-8") as f:
            json.dump({"query": query, "timestamp": now_ts, "results": results}, f, ensure_ascii=False, indent=2)
        logger.info(f"💾 完整 Raw Data 已保存至: {raw_file}")
    except Exception as e:
        logger.error(f"❌ 保存 Raw Data 失败: {e}")

    inserted_count = 0
    platform_stats = {"bilibili": 0, "xiaohongshu": 0, "douyin": 0, "weibo": 0, "web_news": 0}
    
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
        platform_key = ""
        import json
        extra_info_str = json.dumps(r, ensure_ascii=False)
        
        if "bilibili.com" in url:
            import hashlib
            video_id = "anspire_" + hashlib.md5(url.encode()).hexdigest()[:16]
            sql = "INSERT INTO bilibili_video (title, \"desc\", video_id, video_url, create_time, nickname, extra_info) VALUES (:t, :d, :vid, :u, :ts, :a, :ei)"
            params = {"t": title, "d": content, "vid": video_id, "u": url, "ts": ts, "a": "B站用户", "ei": extra_info_str}
            platform_key = "bilibili"
        elif "xiaohongshu.com" in url:
            sql = "INSERT INTO xhs_note (title, \"desc\", note_url, time, nickname, extra_info) VALUES (:t, :d, :u, :ts, :a, :ei)"
            params = {"t": title, "d": content, "u": url, "ts": ts, "a": "小红书用户", "ei": extra_info_str}
            platform_key = "xiaohongshu"
        elif "douyin.com" in url:
            sql = "INSERT INTO douyin_aweme (title, \"desc\", aweme_url, create_time, nickname, extra_info) VALUES (:t, :d, :u, :ts, :a, :ei)"
            params = {"t": title, "d": content, "u": url, "ts": ts, "a": "抖音用户", "ei": extra_info_str}
            platform_key = "douyin"
        elif "weibo.com" in url:
            sql = "INSERT INTO weibo_note (content, note_url, create_time, nickname, extra_info) VALUES (:d, :u, :ts, :a, :ei)"
            params = {"d": content, "u": url, "ts": ts, "a": "微博用户", "ei": extra_info_str}
            platform_key = "weibo"
        else:
            import hashlib
            news_id = "anspire_" + hashlib.md5(url.encode()).hexdigest()[:16]
            crawl_date = datetime.now().date()
            # 其他全部入每日热点表，标记平台为 web
            sql = "INSERT INTO daily_news (news_id, source_platform, title, description, url, crawl_date, add_ts, last_modify_ts, rank_position, extra_info) VALUES (:nid, 'web', :t, :d, :u, :cdate, :ts, :ts, 99, :ei)"
            params = {"nid": news_id, "t": title, "d": content, "u": url, "cdate": crawl_date, "ts": ts, "ei": extra_info_str}
            platform_key = "web_news"
            
        try:
            _run_async(execute_write(sql, params))
            inserted_count += 1
            platform_stats[platform_key] += 1
            
            # 单条记录逐一写入 crawler.log
            try:
                log_file = Path("logs/crawler.log")
                if log_file.parent.exists():
                    with open(log_file, "a", encoding="utf-8") as f:
                        ts_str = datetime.now().strftime('%H:%M:%S')
                        short_title = title[:30] + '...' if len(title) > 30 else title
                        if not short_title:
                            short_title = content[:30] + '...' if len(content) > 30 else content
                        f.write(f"[{ts_str}] [RECORD] 📝 成功插入 -> [{platform_key}] {short_title}\n")
            except Exception:
                pass
                
        except Exception as e:
            # 表可能不存在或主键冲突，静默忽略
            logger.debug(f"插入数据失败: {e}")
            pass
        finally:
            import InsightEngine.utils.db as db_utils
            db_utils._engine = None  # Prevent event loop reuse issues
            
    details = ", ".join([f"{k}: {v}条" for k, v in platform_stats.items() if v > 0])
    logger.info(f"✅ 增量数据抓取完成: 成功向本地数据库插入 {inserted_count} 条最新记录. 分布: {details}")
    return inserted_count, details
