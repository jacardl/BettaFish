from InsightEngine.tools.search import MediaCrawlerDB, print_response_summary
from loguru import logger

def test_search():
    logger.info("=== 测试 Insight Engine 实时检索工具 ===")
    try:
        db_agent_tools = MediaCrawlerDB()
        
        # 测试场景1: 全局话题搜索
        logger.info("1. 测试: 全局话题搜索 'DeepSeek'")
        res_global = db_agent_tools.search_topic_globally(topic="DeepSeek", limit_per_table=2)
        print_response_summary(res_global)
        
        # 测试场景2: 平台定向搜索
        logger.info("2. 测试: 平台定向搜索 '知乎' 上的 'AI大模型'")
        res_platform = db_agent_tools.search_topic_on_platform(platform='zhihu', topic="AI大模型", limit=2)
        print_response_summary(res_platform)
        
        logger.info("=== 测试完成 ===")
    except Exception as e:
        logger.error(f"测试失败: {e}")

if __name__ == "__main__":
    test_search()