import os
import sys
from pathlib import Path

# 将项目根目录加入环境变量，以便导入其他模块
sys.path.insert(0, os.path.abspath(os.path.dirname(__file__)))

from InsightEngine.utils.data_ingestion import ingest_seed_data
from InsightEngine.tools.search import MediaCrawlerDB
from InsightEngine.utils.db import execute_write, _run_async

def main():
    seed_id = "test_embedding_999"
    seed_dir = Path("final_reports/seeds")
    seed_dir.mkdir(parents=True, exist_ok=True)
    seed_file = seed_dir / f"{seed_id}.txt"
    
    # 1. 伪造一段故意不用搜索词原话的文本
    original_text = "这款新作的动作系统设计非常特别，制作组显然没有考虑到新手的感受，游戏难度太高，让很多手残玩家在第一关就望而却步，但在特定的小圈子里却备受好评。"
    seed_file.write_text(original_text, encoding="utf-8")
    print(f"✅ [1/4] 创建测试 Seed 文件完成: {seed_file.name}")
    print(f"   原文内容: '{original_text}'\n")
    
    # 2. 模拟入库流程 (会触发 Embedding 向量计算)
    print("⏳ [2/4] 正在进行 Seed 数据切片与入库 (计算向量中)...")
    ingest_seed_data(seed_id)
    print("   入库与向量化完成。\n")
    
    # 3. 模拟各引擎从本地数据库通过“意译”词汇搜索
    search_query = "非常硬核的游戏"
    print(f"🔍 [3/4] 开始跨表语义检索: '{search_query}'")
    print(f"   (注意：原文并没有“硬核”这两个字，如果能搜到说明 Embedding 语义匹配成功！)\n")
    
    db_tool = MediaCrawlerDB()
    # 调用刚改造的本地向量搜索逻辑
    response = db_tool.search_topic_globally(search_query, limit_per_table=3)
    
    # 4. 展示与验证结果
    print("📊 [4/4] 检索结果:")
    found = False
    if hasattr(response, 'data'):
        for item in response.data:
            title = item.get('title', '')
            content = item.get('content', '')
            platform = item.get('platform', '')
            
            # 判断是否精准命中了我们刚才伪造的片段
            if platform == 'seed' and seed_id[:8] in item.get('url', ''):
                print(f"   🎯 【命中目标！】\n      [平台]: {platform}\n      [标题]: {title}\n      [内容]: {content}")
                print("      👉 匹配成功：系统成功理解了“硬核”等同于原文的“难度太高/手残望而却步”！")
                found = True
            else:
                # 打印出其他被一并检索出来的相关资讯
                short_content = content.replace('\n', ' ')[:50] + "..." if len(content) > 50 else content
                print(f"   - [其他结果] 标题: {title} | 内容片段: {short_content}")
                
    if not found:
        print("   ❌ 未能命中目标文本。")
        
    # 5. 清理测试产生的脏数据
    print("\n🧹 清理测试环境...")
    _run_async(execute_write("DELETE FROM daily_news WHERE news_id LIKE :nid", {"nid": f"seed_{seed_id[:8]}%"}))
    if seed_file.exists():
        seed_file.unlink()
    print("✅ 清理完成。测试脚本运行结束。")

if __name__ == "__main__":
    main()
