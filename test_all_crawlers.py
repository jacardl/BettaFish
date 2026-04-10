import os
import sys
from dotenv import load_dotenv

# 加载环境变量
load_dotenv()

# 添加项目根目录到 Python 路径
sys.path.insert(0, os.path.abspath(os.path.dirname(__file__)))

from InsightEngine.utils.data_ingestion import (
    ingest_incremental_tavily_data,
    ingest_incremental_bocha_data,
    ingest_incremental_web_access_data
)

# 动态尝试导入 anspire，如果没有被剔除
try:
    from InsightEngine.utils.data_ingestion import ingest_incremental_anspire_data
except ImportError:
    ingest_incremental_anspire_data = None

def test_all_crawlers():
    query = "AI大模型最新突破 2026"
    print(f"=== 开始全量测试四大爬虫引擎，搜索词: '{query}' ===")
    
    print("\n[1/4] 测试 Anspire...")
    if ingest_incremental_anspire_data:
        try:
            count, detail = ingest_incremental_anspire_data(query)
            print(f"✅ Anspire 结果: 成功插入 {count} 条")
            print(f"   分布详情: {detail}")
        except Exception as e:
            print(f"❌ Anspire 异常: {e}")
    else:
        print("❌ Anspire 被跳过 (未导入)")
        
    print("\n[2/4] 测试 Tavily...")
    try:
        count, detail = ingest_incremental_tavily_data(query)
        print(f"✅ Tavily 结果: 成功插入 {count} 条")
        print(f"   分布详情: {detail}")
    except Exception as e:
        print(f"❌ Tavily 异常: {e}")
        
    print("\n[3/4] 测试 Bocha...")
    try:
        count, detail = ingest_incremental_bocha_data(query)
        print(f"✅ Bocha 结果: 成功插入 {count} 条")
        print(f"   分布详情: {detail}")
    except Exception as e:
        print(f"❌ Bocha 异常: {e}")
        
    print("\n[4/4] 测试 Firecrawl (Web-Access)...")
    try:
        count, detail = ingest_incremental_web_access_data(query)
        print(f"✅ Firecrawl 结果: 成功插入 {count} 条")
        print(f"   分布详情: {detail}")
    except Exception as e:
        print(f"❌ Firecrawl 异常: {e}")
        
    print("\n=== 测试完成 ===")

if __name__ == "__main__":
    test_all_crawlers()
