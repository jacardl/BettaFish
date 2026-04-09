import asyncio
from InsightEngine.utils.db import get_async_engine
from sqlalchemy import text

async def main():
    engine = get_async_engine()
    async with engine.connect() as conn:
        print("=== Seed 数据 (daily_news 中 platform 为 'seed_document') ===")
        res = await conn.execute(text("SELECT news_id, title, description, add_ts FROM daily_news WHERE source_platform = 'seed_document' ORDER BY add_ts DESC LIMIT 5"))
        for r in res:
            print(f"ID: {r[0]}, Title: {r[1]}")
            desc = r[2][:100] + '...' if r[2] else 'None'
            print(f"Desc: {desc}")
            print("-" * 20)
            
        print("\n=== 最近新增的 Anspire 爬取数据 (今天的数据) ===")
        # 获取当天开始的时间戳
        import datetime
        today_ts = int(datetime.datetime.now().replace(hour=0, minute=0, second=0, microsecond=0).timestamp() * 1000)
        
        tables = [
            ('daily_news', 'add_ts', 'title'),
            ('xhs_note', 'time', 'title'),
            ('bilibili_video', 'create_time', 'title'),
            ('douyin_aweme', 'create_time', 'title'),
            ('weibo_note', 'create_time', 'content')
        ]
        
        for t, ts_col, title_col in tables:
            try:
                res = await conn.execute(text(f"SELECT {title_col}, {ts_col} FROM {t} WHERE {ts_col} >= {today_ts} ORDER BY {ts_col} DESC LIMIT 5"))
                rows = list(res)
                if rows:
                    print(f"--- 表 {t} ({len(rows)}条记录) ---")
                    for r in rows:
                        title = r[0][:50] + '...' if r[0] else 'None'
                        print(f"Title: {title}")
            except Exception as e:
                pass
                
asyncio.run(main())