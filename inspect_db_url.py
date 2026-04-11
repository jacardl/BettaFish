import asyncio
from InsightEngine.utils.db import get_async_engine
from sqlalchemy import text

async def main():
    engine = get_async_engine()
    async with engine.connect() as conn:
        res = await conn.execute(text("SELECT table_name, column_name FROM information_schema.columns WHERE table_name IN ('daily_news', 'xhs_note', 'bilibili_video', 'douyin_aweme', 'weibo_note', 'zhihu_content', 'tieba_note') AND column_name LIKE '%url%'"))
        print(res.fetchall())

if __name__ == "__main__":
    asyncio.run(main())