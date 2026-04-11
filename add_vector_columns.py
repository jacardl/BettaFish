import asyncio
from InsightEngine.utils.db import execute_write

async def main():
    try:
        # Create extension vector
        await execute_write("CREATE EXTENSION IF NOT EXISTS vector;")
        print("Enabled pgvector extension.")

        # Add embedding column to tables if not exists
        tables = [
            "daily_news",
            "xhs_note",
            "bilibili_video",
            "douyin_aweme",
            "weibo_note",
            "zhihu_content",
            "tieba_note",
            "kuaishou_video",
            "xhs_note_comment",
            "douyin_aweme_comment",
            "bilibili_video_comment",
            "weibo_note_comment",
            "zhihu_comment",
            "tieba_comment"
        ]

        for table in tables:
            try:
                await execute_write(f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS embedding vector(768);")
                print(f"Added embedding column to {table}.")
            except Exception as e:
                print(f"Table {table} might not exist yet: {e}")
                
    except Exception as e:
        print(f"Error enabling vector: {e}")

if __name__ == "__main__":
    asyncio.run(main())
