import asyncio
from InsightEngine.utils.data_ingestion import _insert_results_into_db
import time

results = []
for i in range(100):
    results.append({
        "url": f"https://www.xiaohongshu.com/explore/test_{i}",
        "title": f"测试笔记_{i}",
        "content": f"这是一个测试小红书笔记_{i}。测试内容很多很多。",
        "time": "2024-04-10T12:00:00Z"
    })

now_ts = int(time.time() * 1000)
start_time = time.time()
inserted_count, details = _insert_results_into_db(results, now_ts, "test_source")
end_time = time.time()

print(f"Inserted {inserted_count} rows in {end_time - start_time:.2f} seconds.")
print(f"Details: {details}")
