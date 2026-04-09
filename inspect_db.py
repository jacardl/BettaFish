import asyncio
from InsightEngine.utils.db import get_async_engine
from sqlalchemy import text

async def main():
    engine = get_async_engine()
    async with engine.connect() as conn:
        for t in ['daily_news', 'xhs_note', 'bilibili_video', 'douyin_aweme', 'weibo_note']:
            res = await conn.execute(text(f"SELECT column_name FROM information_schema.columns WHERE table_name = '{t}' AND is_nullable = 'NO'"))
            print(f"{t} NOT NULL cols:", [r[0] for r in res])

asyncio.run(main())