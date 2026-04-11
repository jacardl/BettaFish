import asyncio
from InsightEngine.utils.db import execute_write

async def main():
    try:
        affected = await execute_write("DELETE FROM xhs_note WHERE title LIKE '测试笔记_%'")
        print(f"Deleted {affected} test notes from xhs_note.")
    except Exception as e:
        print(f"Error: {e}")

if __name__ == "__main__":
    asyncio.run(main())
