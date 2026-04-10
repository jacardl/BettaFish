import os
import sys
from dotenv import load_dotenv

# 加载环境变量
load_dotenv()

# 添加项目根目录到 Python 路径
sys.path.insert(0, os.path.abspath(os.path.dirname(__file__)))

from InsightEngine.utils.data_ingestion import (
    ingest_incremental_bocha_data,
    ingest_incremental_web_access_data
)

def test_bocha_firecrawl():
    query = "AI大模型最新突破 2026"
    print(f"=== 开始测试 Bocha 和 Firecrawl，搜索词: '{query}' ===")
    
    print("\n[1/2] 测试 Bocha...")
    try:
        count, detail = ingest_incremental_bocha_data(query)
        print(f"✅ Bocha 结果: 成功插入 {count} 条")
        print(f"   分布详情: {detail}")
    except Exception as e:
        print(f"❌ Bocha 异常: {e}")
        
    print("\n[2/2] 测试 Firecrawl (Web-Access)...")
    try:
        count, detail = ingest_incremental_web_access_data(query)
        print(f"✅ Firecrawl 结果: 成功插入 {count} 条")
        print(f"   分布详情: {detail}")
    except Exception as e:
        print(f"❌ Firecrawl 异常: {e}")
        
    print("\n=== 测试完成 ===")

if __name__ == "__main__":
    test_bocha_firecrawl()
