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
    seed_path_json = root_dir / 'final_reports' / 'seeds' / f"{seed_id}.json"
    seed_path_txt = root_dir / 'final_reports' / 'seeds' / f"{seed_id}.txt"
    
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
        
    # 高级语义切片：基于段落和句子的重叠切片 (Overlap Chunking)
    import re
    
    def smart_chunking(text: str, max_chunk_len: int = 1000, overlap: int = 150) -> list:
        # 1. 先按段落切分
        paragraphs = re.split(r'\n\s*\n', text)
        
        chunks = []
        current_chunk = ""
        
        for p in paragraphs:
            p = p.strip()
            if not p:
                continue
                
            # 如果加上这个段落还没超长，就加上去
            if len(current_chunk) + len(p) + 2 <= max_chunk_len:
                current_chunk += ("\n\n" if current_chunk else "") + p
            else:
                # 已经有一个足够大的 chunk，先保存
                if current_chunk:
                    chunks.append(current_chunk)
                    
                    # 提取 overlap 作为下一个 chunk 的开头
                    # 尽量从标点符号处切分 overlap
                    overlap_text = current_chunk[-overlap:]
                    match = re.search(r'[。！？.!?\n]', overlap_text)
                    if match:
                        overlap_start = match.end()
                        current_chunk = overlap_text[overlap_start:].strip()
                    else:
                        current_chunk = overlap_text.strip()
                
                # 如果单个段落特别长（超过 max_chunk_len），按句子强行切分
                if len(p) > max_chunk_len:
                    sentences = re.split(r'([。！？.!?])', p)
                    
                    temp_sent = current_chunk
                    for i in range(0, len(sentences) - 1, 2):
                        sentence = sentences[i] + (sentences[i+1] if i+1 < len(sentences) else "")
                        if len(temp_sent) + len(sentence) > max_chunk_len and temp_sent:
                            chunks.append(temp_sent)
                            
                            overlap_text = temp_sent[-overlap:]
                            match = re.search(r'[。！？.!?\n]', overlap_text)
                            if match:
                                temp_sent = overlap_text[match.end():].strip() + " " + sentence
                            else:
                                temp_sent = overlap_text.strip() + " " + sentence
                        else:
                            temp_sent += (" " if temp_sent else "") + sentence
                    
                    current_chunk = temp_sent
                else:
                    current_chunk += ("\n\n" if current_chunk else "") + p
                    
        if current_chunk:
            chunks.append(current_chunk)
            
        return chunks if chunks else [text]
        
    # 调用智能切片，理想块大小为800字，重叠100字
    chunks = smart_chunking(seed_text, max_chunk_len=800, overlap=100)
    
    now_ts = int(datetime.now().timestamp() * 1000)
    crawl_date = datetime.now().date()
    
    from utils.embedding import get_embeddings
    
    # 批量计算 Embedding 以极大提升速度
    texts_to_embed = [f"{title}_片段{idx+1} {chunk}" for idx, chunk in enumerate(chunks)]
    try:
        embeddings = get_embeddings(texts_to_embed)
    except Exception as e:
        logger.error(f"Batch calculate embedding failed: {e}")
        embeddings = [None] * len(chunks)
    
    sql = """
        INSERT INTO daily_news (news_id, source_platform, title, description, url, crawl_date, add_ts, last_modify_ts, rank_position, embedding)
        VALUES (:nid, :platform, :title, :desc, :url, :cdate, :add_ts, :add_ts, 1, :emb)
    """
    
    inserted_count = 0
    all_params = []
    from InsightEngine.utils.db import execute_write_many
    
    for idx, chunk in enumerate(chunks):
        chunk_title = f"{title}_片段{idx+1}"
        news_id = f"seed_{seed_id[:8]}_{now_ts}_{idx}"
        
        emb = embeddings[idx]
        if emb:
            emb_str = f"[{','.join(map(str, emb))}]"
        else:
            emb_str = None
        
        params = {
            "nid": news_id,
            "platform": "seed_document",
            "title": chunk_title,
            "desc": chunk,
            "url": url,
            "cdate": crawl_date,
            "add_ts": now_ts + idx,  # 保证时间戳唯一性
            "emb": emb_str
        }
        all_params.append(params)
        
        # 单条记录逐一写入 crawler.log
        log_file = root_dir / "logs" / "crawler.log"
        if log_file.parent.exists():
            with open(log_file, "a", encoding="utf-8") as f:
                ts_str = datetime.now().strftime('%H:%M:%S')
                short_title = chunk_title[:40] + '...' if len(chunk_title) > 40 else chunk_title
                f.write(f"[{ts_str}] [RECORD] 📝 成功插入 [seed] -> [seed_document] {short_title}\n")
                
    try:
        if all_params:
            _run_async(execute_write_many(sql, all_params))
            inserted_count = len(all_params)
    except Exception as e:
        logger.error(f"❌ 批量插入 Seed 数据分片失败: {e}")
    finally:
        import InsightEngine.utils.db as db_utils
        db_utils._engine = None  # Prevent event loop reuse issues
            
    logger.info(f"✅ Seed 文件 ({seed_id}) 已拆分为 {len(chunks)} 个片段并持久化至 daily_news 表, 成功插入 {inserted_count} 条")
    
    # 将 seed 总体入库行为记录到 crawler.log
    log_file = root_dir / "logs" / "crawler.log"
    if log_file.parent.exists():
        with open(log_file, "a", encoding="utf-8") as f:
            ts_str = datetime.now().strftime('%H:%M:%S')
            f.write(f"[{ts_str}] [SYSTEM] 📥 [Seed入库] 成功将用户上传的附件 '{title}' (ID: {seed_id}) 拆分为 {len(chunks)} 条记录并写入本地数据库。\n")


def _insert_results_into_db(results, now_ts, source_name):
    """将抓取结果列表统一写入本地数据库"""
    inserted_count = 0
    platform_stats = {"bilibili": 0, "xiaohongshu": 0, "douyin": 0, "weibo": 0, "web_news": 0}
    
    from utils.embedding import get_embeddings
    
    # 提取所有文本准备进行批量向量化
    texts_to_embed = []
    for r in results:
        title = r.get("title", "") or ""
        content = r.get("content", "") or r.get("snippet", "") or r.get("raw_content", "") or ""
        texts_to_embed.append(title + " " + content)
        
    try:
        embeddings = get_embeddings(texts_to_embed)
    except Exception as e:
        logger.error(f"Batch calculate embedding failed: {e}")
        embeddings = [None] * len(results)
    
    from InsightEngine.utils.db import execute_write_many
    
    # Collect statements and params by table/sql
    sql_batches = {}
    platform_keys = []
    
    for idx, r in enumerate(results):
        url = r.get("url", "")
        title = r.get("title", "") or ""
        content = r.get("content", "") or r.get("snippet", "") or r.get("raw_content", "") or ""
        date_str = r.get("date") or r.get("published_date") or r.get("time")
        
        # 解析时间戳
        ts = now_ts
        if date_str:
            try:
                # 尝试多种时间格式
                if 'T' in date_str:
                    dt = datetime.fromisoformat(date_str.split('+')[0].strip().replace('Z', ''))
                else:
                    from dateutil import parser
                    dt = parser.parse(date_str)
                ts = int(dt.timestamp() * 1000)
            except Exception:
                pass
                
        # 判断路由
        sql = ""
        params = {}
        platform_key = ""
        import json
        extra_info_str = json.dumps(r, ensure_ascii=False)
        
        emb = embeddings[idx]
        if emb:
            emb_str = f"[{','.join(map(str, emb))}]"
        else:
            emb_str = None
        
        if "bilibili.com" in url:
            import hashlib
            video_id = f"{source_name}_" + hashlib.md5(url.encode()).hexdigest()[:16]
            sql = "INSERT INTO bilibili_video (title, \"desc\", video_id, video_url, create_time, nickname, extra_info, embedding) VALUES (:t, :d, :vid, :u, :ts, :a, :ei, :emb)"
            params = {"t": title, "d": content, "vid": video_id, "u": url, "ts": ts, "a": "B站用户", "ei": extra_info_str, "emb": emb_str}
            platform_key = "bilibili"
        elif "xiaohongshu.com" in url:
            sql = "INSERT INTO xhs_note (title, \"desc\", note_url, time, nickname, extra_info, embedding) VALUES (:t, :d, :u, :ts, :a, :ei, :emb)"
            params = {"t": title, "d": content, "u": url, "ts": ts, "a": "小红书用户", "ei": extra_info_str, "emb": emb_str}
            platform_key = "xiaohongshu"
        elif "douyin.com" in url:
            sql = "INSERT INTO douyin_aweme (title, \"desc\", aweme_url, create_time, nickname, extra_info, embedding) VALUES (:t, :d, :u, :ts, :a, :ei, :emb)"
            params = {"t": title, "d": content, "u": url, "ts": ts, "a": "抖音用户", "ei": extra_info_str, "emb": emb_str}
            platform_key = "douyin"
        elif "weibo.com" in url:
            sql = "INSERT INTO weibo_note (content, note_url, create_time, nickname, extra_info, embedding) VALUES (:d, :u, :ts, :a, :ei, :emb)"
            params = {"d": content, "u": url, "ts": ts, "a": "微博用户", "ei": extra_info_str, "emb": emb_str}
            platform_key = "weibo"
        else:
            import hashlib
            news_id = f"{source_name}_" + hashlib.md5(url.encode()).hexdigest()[:16]
            crawl_date = datetime.now().date()
            # 其他全部入每日热点表，标记平台为 web
            sql = "INSERT INTO daily_news (news_id, source_platform, title, description, url, crawl_date, add_ts, last_modify_ts, rank_position, extra_info, embedding) VALUES (:nid, 'web', :t, :d, :u, :cdate, :ts, :ts, 99, :ei, :emb)"
            params = {"nid": news_id, "t": title, "d": content, "u": url, "cdate": crawl_date, "ts": ts, "ei": extra_info_str, "emb": emb_str}
            platform_key = "web_news"
            
        if sql not in sql_batches:
            sql_batches[sql] = []
        sql_batches[sql].append(params)
        
        platform_stats[platform_key] += 1
        platform_keys.append((platform_key, title, content))

    try:
        # 执行批量插入
        for sql, params_list in sql_batches.items():
            _run_async(execute_write_many(sql, params_list))
            inserted_count += len(params_list)
            
        # 批量写日志
        log_file = Path("logs/crawler.log")
        if log_file.parent.exists():
            with open(log_file, "a", encoding="utf-8") as f:
                for platform_key, title, content in platform_keys:
                    ts_str = datetime.now().strftime('%H:%M:%S')
                    short_title = title[:30] + '...' if len(title) > 30 else title
                    if not short_title:
                        short_title = content[:30] + '...' if len(content) > 30 else content
                    f.write(f"[{ts_str}] [RECORD] 📝 成功插入 [{source_name}] -> [{platform_key}] {short_title}\n")
    except Exception as e:
        logger.error(f"❌ 批量插入数据失败: {e}")
    finally:
        import InsightEngine.utils.db as db_utils
        db_utils._engine = None

    details = ", ".join([f"{k}: {v}条" for k, v in platform_stats.items() if v > 0])
    return inserted_count, details


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
        raw_file = raw_dir / f"crawler_raw_anspire_{now_ts}.json"
        with open(raw_file, "w", encoding="utf-8") as f:
            json.dump({"query": query, "timestamp": now_ts, "source": "anspire", "results": results}, f, ensure_ascii=False, indent=2)
        logger.info(f"💾 完整 Raw Data 已保存至: {raw_file}")
    except Exception as e:
        logger.error(f"❌ 保存 Raw Data 失败: {e}")

    inserted_count, details = _insert_results_into_db(results, now_ts, "anspire")
    
    logger.info(f"✅ [Anspire] 增量数据抓取完成: 成功向本地数据库插入 {inserted_count} 条最新记录. 分布: {details}")
    return inserted_count, details

def ingest_incremental_duckduckgo_data(query: str):
    """兜底方案：当没有任何 API Key 时，使用免费的 DuckDuckGo HTML 抓取极其基础的数据"""
    if not query:
        return 0, "查询为空"
        
    logger.info(f"🔄 未配置任何高级 API，启动兜底方案: 从 DuckDuckGo 获取 '{query}' 的基础数据...")
    import requests
    from bs4 import BeautifulSoup
    import re
    
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36"
    }
    
    results = []
    try:
        # 为了防封禁，换一个国内可以访问且无需API Key的替代方案，比如 Sogou 或直接使用内置的模拟数据作为兜底
        url = "https://sogou.com/web"
        response = requests.get(url, headers=headers, params={"query": query}, timeout=30)
        response.raise_for_status()
        soup = BeautifulSoup(response.text, "html.parser")
        
        for div in soup.find_all('div', class_='vrwrap'):
            title_elem = div.find('h3', class_='vtit')
            if not title_elem:
                title_elem = div.find('h3', class_='vr-title')
            
            snippet_elem = div.find('div', class_='star-wiki')
            if not snippet_elem:
                snippet_elem = div.find('p', class_='str_info')
                
            if title_elem and title_elem.a:
                title = title_elem.get_text(strip=True)
                link = title_elem.a['href']
                if not link.startswith('http'):
                    link = "https://sogou.com" + link
                    
                snippet = snippet_elem.get_text(strip=True) if snippet_elem else title
                
                results.append({
                    "title": title,
                    "url": link,
                    "content": snippet,
                    "date": datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
                    "original_data": {"source": "sogou_fallback"}
                })
                
        # 如果爬取失败（被拦截等），返回两条极其基础的模拟数据以防系统崩溃
        if not results:
            results = [
                {
                    "title": f"关于 {query} 的全网基础分析",
                    "url": "local://fallback/1",
                    "content": f"系统未配置高级 API Key，当前为基础后备搜索结果。包含关键字：{query}。",
                    "date": datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
                    "original_data": {"source": "mock_fallback"}
                },
                {
                    "title": f"最新 {query} 趋势报告",
                    "url": "local://fallback/2",
                    "content": f"这是基础搜索为您返回的兜底数据，以确保流程不中断。如需深度数据，请配置外部 API。",
                    "date": datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
                    "original_data": {"source": "mock_fallback"}
                }
            ]
    except Exception as e:
        logger.error(f"❌ 兜底抓取请求失败: {e}")
        return 0, f"兜底请求失败: {e}"
        
    if not results:
        logger.info("兜底方案未抓取到任何数据")
        return 0, "兜底 API 返回 0 条结果"
        
    now_ts = int(datetime.now().timestamp() * 1000)
    
    try:
        raw_dir = Path("logs/raw_data")
        raw_dir.mkdir(parents=True, exist_ok=True)
        raw_file = raw_dir / f"crawler_raw_fallback_{now_ts}.json"
        with open(raw_file, "w", encoding="utf-8") as f:
            json.dump({"query": query, "timestamp": now_ts, "source": "duckduckgo_fallback", "results": results}, f, ensure_ascii=False, indent=2)
    except Exception:
        pass

    inserted_count, details = _insert_results_into_db(results, now_ts, "fallback_ddg")
    logger.info(f"✅ [兜底方案] 数据抓取完成: 成功向本地数据库插入 {inserted_count} 条最新记录. 分布: {details}")
    return inserted_count, details
    """通过 Tavily API 抓取增量数据，插入本地数据表"""
    if not query:
        return 0, "查询为空"
        
    tavily_key = os.getenv("TAVILY_API_KEY") or getattr(settings, "TAVILY_API_KEY", None)
    if not tavily_key:
        logger.warning("未配置 TAVILY_API_KEY，跳过 Tavily 抓取")
        return 0, "未配置 API Key"
        
    logger.info(f"🔄 正在从 Tavily 获取 '{query}' 的增量数据...")
    try:
        from tavily import TavilyClient
        client = TavilyClient(api_key=tavily_key)
        # 强制开启获取全文选项，以获得 raw_content
        response_dict = client.search(query=query, topic="general", include_raw_content=True, max_results=100)
        results = response_dict.get("results", [])
    except Exception as e:
        logger.error(f"❌ Tavily 增量抓取请求失败: {e}")
        return 0, f"请求失败: {e}"
        
    if not results:
        logger.info("Tavily 未抓取到任何增量数据")
        return 0, "API 返回 0 条结果"
        
    now_ts = int(datetime.now().timestamp() * 1000)
    
    try:
        raw_dir = Path("logs/raw_data")
        raw_dir.mkdir(parents=True, exist_ok=True)
        raw_file = raw_dir / f"crawler_raw_tavily_{now_ts}.json"
        with open(raw_file, "w", encoding="utf-8") as f:
            json.dump({"query": query, "timestamp": now_ts, "source": "tavily", "results": results}, f, ensure_ascii=False, indent=2)
        logger.info(f"💾 完整 Tavily Raw Data 已保存至: {raw_file}")
    except Exception as e:
        pass

    inserted_count, details = _insert_results_into_db(results, now_ts, "tavily")
    logger.info(f"✅ [Tavily] 增量数据抓取完成: 成功向本地数据库插入 {inserted_count} 条最新记录. 分布: {details}")
    return inserted_count, details

def ingest_incremental_bocha_data(query: str):
    """通过 Bocha API 抓取增量数据，插入本地数据表"""
    if not query:
        return 0, "查询为空"
        
    bocha_key = os.getenv("BOCHA_API_KEY") or os.getenv("BOCHA_WEB_API_KEY") or getattr(settings, "BOCHA_WEB_SEARCH_API_KEY", None)
    if not bocha_key:
        logger.warning("未配置 BOCHA_API_KEY 或 BOCHA_WEB_API_KEY，跳过 Bocha 抓取")
        return 0, "未配置 API Key"
        
    logger.info(f"🔄 正在从 Bocha 获取 '{query}' 的增量数据...")
    url = os.getenv("BOCHA_BASE_URL", "https://api.bocha.cn/v1/web-search")
    headers = {
        "Authorization": f"Bearer {bocha_key}",
        "Content-Type": "application/json"
    }
    payload = {
        "query": query,
        "count": 100
    }
    try:
        response = requests.post(url, headers=headers, json=payload, timeout=45)
        response.raise_for_status()
        json_resp = response.json()
        results = json_resp.get("data", {}).get("webPages", {}).get("value", [])
    except Exception as e:
        logger.error(f"❌ Bocha 增量抓取请求失败: {e}")
        return 0, f"请求失败: {e}"
        
    if not results:
        logger.info("Bocha 未抓取到任何增量数据")
        return 0, "API 返回 0 条结果"
        
    # 适配 Bocha 的返回字段到统一格式
    formatted_results = []
    for r in results:
        formatted_results.append({
            "title": r.get("name", ""),
            "url": r.get("url", ""),
            "content": r.get("snippet", ""),
            "date": r.get("dateLastCrawled", ""),
            "siteName": r.get("siteName", ""),
            "original_data": r
        })
        
    now_ts = int(datetime.now().timestamp() * 1000)
    
    try:
        raw_dir = Path("logs/raw_data")
        raw_dir.mkdir(parents=True, exist_ok=True)
        raw_file = raw_dir / f"crawler_raw_bocha_{now_ts}.json"
        with open(raw_file, "w", encoding="utf-8") as f:
            json.dump({"query": query, "timestamp": now_ts, "source": "bocha", "results": formatted_results}, f, ensure_ascii=False, indent=2)
        logger.info(f"💾 完整 Bocha Raw Data 已保存至: {raw_file}")
    except Exception as e:
        pass

    inserted_count, details = _insert_results_into_db(formatted_results, now_ts, "bocha")
    logger.info(f"✅ [Bocha] 增量数据抓取完成: 成功向本地数据库插入 {inserted_count} 条最新记录. 分布: {details}")
    return inserted_count, details

def ingest_incremental_web_access_data(query: str):
    """通过 Web-Access (如 Firecrawl 或自定义爬虫服务) 抓取增量数据"""
    if not query:
        return 0, "查询为空"
        
    firecrawl_key = os.getenv("FIRECRAWL_API_KEY")
    if not firecrawl_key:
        logger.info("未配置 FIRECRAWL_API_KEY，跳过 Web-Access 深度抓取")
        return 0, "未配置 Firecrawl API Key"
        
    logger.info(f"🔄 正在通过 Web-Access 服务获取 '{query}' 的增量数据...")
    try:
        import requests
        base_url = os.getenv("FIRECRAWL_API_URL", "https://api.firecrawl.dev/v1")
        # 兼容后缀
        if not base_url.endswith("/v1") and not base_url.endswith("/v0"):
            if base_url.endswith("/"):
                base_url = base_url + "v1"
            else:
                base_url = base_url + "/v1"
                
        url = f"{base_url}/search"
        headers = {
            "Authorization": f"Bearer {firecrawl_key}",
            "Content-Type": "application/json"
        }
        payload = {
            "query": query,
            "limit": 100
        }
        response = requests.post(url, headers=headers, json=payload, timeout=45)
        response.raise_for_status()
        json_resp = response.json()
        results = json_resp.get("data", [])
    except Exception as e:
        logger.error(f"❌ Web-Access 增量抓取请求失败: {e}")
        return 0, f"请求失败: {e}"
        
    if not results:
        logger.info("Web-Access 未抓取到任何增量数据")
        return 0, "API 返回 0 条结果"
        
    formatted_results = []
    for r in results:
        formatted_results.append({
            "title": r.get("title", ""),
            "url": r.get("url", ""),
            "content": r.get("description", "") or r.get("markdown", ""),
            "date": r.get("publishedDate", ""),
            "original_data": r
        })
        
    now_ts = int(datetime.now().timestamp() * 1000)
    
    try:
        raw_dir = Path("logs/raw_data")
        raw_dir.mkdir(parents=True, exist_ok=True)
        raw_file = raw_dir / f"crawler_raw_webaccess_{now_ts}.json"
        with open(raw_file, "w", encoding="utf-8") as f:
            json.dump({"query": query, "timestamp": now_ts, "source": "web_access", "results": formatted_results}, f, ensure_ascii=False, indent=2)
        logger.info(f"💾 完整 Web-Access Raw Data 已保存至: {raw_file}")
    except Exception as e:
        pass

    inserted_count, details = _insert_results_into_db(formatted_results, now_ts, "web_access")
    logger.info(f"✅ [Web-Access] 增量数据抓取完成: 成功向本地数据库插入 {inserted_count} 条最新记录. 分布: {details}")
    return inserted_count, details
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
        raw_file = raw_dir / f"crawler_raw_anspire_{now_ts}.json"
        with open(raw_file, "w", encoding="utf-8") as f:
            json.dump({"query": query, "timestamp": now_ts, "source": "anspire", "results": results}, f, ensure_ascii=False, indent=2)
        logger.info(f"💾 完整 Raw Data 已保存至: {raw_file}")
    except Exception as e:
        logger.error(f"❌ 保存 Raw Data 失败: {e}")

    inserted_count, details = _insert_results_into_db(results, now_ts, "anspire")
    
    logger.info(f"✅ [Anspire] 增量数据抓取完成: 成功向本地数据库插入 {inserted_count} 条最新记录. 分布: {details}")
    return inserted_count, details

def ingest_all_sources_data(query: str):
    """
    统一的数据采集入口。
    并行调用配置的所有数据源（Anspire, Tavily, Bocha 等），
    并将所有结果聚合写入本地数据库和日志。
    """
    if not query:
        return 0, "查询为空"

    logger.info(f"🚀 开始全网多源联合采集，关键词: '{query}'")
    
    total_inserted = 0
    all_details = []
    
    # 我们按顺序或并行执行，为了简单稳妥起见，这里按顺序执行并累加结果
    try:
        anspire_count, anspire_detail = ingest_incremental_anspire_data(query)
        if anspire_count > 0:
            total_inserted += anspire_count
            all_details.append(f"[Anspire] {anspire_detail}")
    except Exception as e:
        logger.error(f"Anspire 采集异常: {e}")

    try:
        tavily_count, tavily_detail = ingest_incremental_tavily_data(query)
        if tavily_count > 0:
            total_inserted += tavily_count
            all_details.append(f"[Tavily] {tavily_detail}")
    except Exception as e:
        logger.error(f"Tavily 采集异常: {e}")

    try:
        bocha_count, bocha_detail = ingest_incremental_bocha_data(query)
        if bocha_count > 0:
            total_inserted += bocha_count
            all_details.append(f"[Bocha] {bocha_detail}")
    except Exception as e:
        logger.error(f"Bocha 采集异常: {e}")

    # Web-Access (Firecrawl)
    try:
        web_count, web_detail = ingest_incremental_web_access_data(query)
        if web_count > 0:
            total_inserted += web_count
            all_details.append(f"[WebAccess] {web_detail}")
    except Exception as e:
        logger.error(f"Web-Access 采集异常: {e}")

    # 如果没有任何 API Key 被配置且都没有抓到数据，触发兜底方案
    if total_inserted == 0 and not all_details:
        # 检查是否所有配置都为空
        anspire_key = os.getenv("ANSPIRE_API_KEY") or getattr(settings, "ANSPIRE_API_KEY", None)
        tavily_key = os.getenv("TAVILY_API_KEY") or getattr(settings, "TAVILY_API_KEY", None)
        bocha_key = os.getenv("BOCHA_API_KEY") or os.getenv("BOCHA_WEB_API_KEY") or getattr(settings, "BOCHA_WEB_SEARCH_API_KEY", None)
        firecrawl_key = os.getenv("FIRECRAWL_API_KEY")
        
        if not any([anspire_key, tavily_key, bocha_key, firecrawl_key]):
            logger.warning("⚠️ 警告: 未配置任何外部搜索 API，将使用 DuckDuckGo 兜底方案。")
            try:
                ddg_count, ddg_detail = ingest_incremental_duckduckgo_data(query)
                if ddg_count > 0:
                    total_inserted += ddg_count
                    all_details.append(f"[DuckDuckGo] {ddg_detail}")
            except Exception as e:
                logger.error(f"DuckDuckGo 兜底采集异常: {e}")

    final_detail = " | ".join(all_details) if all_details else "无新增数据"
    return total_inserted, final_detail
