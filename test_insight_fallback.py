import os
import sys
import logging
from dataclasses import dataclass
from typing import List

# 将项目根目录加入环境变量，以便导入其他模块
sys.path.insert(0, os.path.abspath(os.path.dirname(__file__)))

from InsightEngine.agent import create_agent

# 设置日志显示
logging.basicConfig(level=logging.INFO, format='%(asctime)s | %(levelname)s | %(message)s')

@dataclass
class MockOptimizedResponse:
    optimized_keywords: List[str]
    search_reasoning: str

def test_fallback():
    print("\n" + "="*50)
    print("测试 InsightEngine 搜索工具缺失参数时的降级机制")
    print("="*50 + "\n")

    # 1. 创建 Agent 实例
    agent = create_agent()
    
    # 2. 模拟大模型的非法返回
    print("🚀 [模拟] 大模型返回了需要调用 'search_topic_on_platform'，但是遗漏了 'platform' 参数。")
    tool_name = "search_topic_on_platform"
    kwargs = {
        "start_date": "2024-01-01",
        "end_date": "2024-12-31"
    }
    
    optimized_response = MockOptimizedResponse(
        optimized_keywords=["达巴水痕之地"],
        search_reasoning="测试容错机制"
    )
    query = "帮我找到达巴水痕之地的最新信息"
    
    # 3. 直接调用 Agent 内部方法
    print("\n🔍 开始执行 execute_search_tool ...")
    try:
        # 为了不经过大模型耗时请求，我们直接在代码里塞进一个伪造的关键词响应
        from InsightEngine.tools.keyword_optimizer import KeywordOptimizationResponse
        from unittest.mock import patch
        
        with patch('InsightEngine.agent.keyword_optimizer.optimize_keywords') as mock_opt:
            mock_opt.return_value = KeywordOptimizationResponse(
                original_query=query,
                optimized_keywords=["达巴水痕之地"],
                reasoning="测试容错机制",
                success=True
            )
            response = agent.execute_search_tool(
                tool_name=tool_name,
                query=query,
                **kwargs
            )
        
        print("\n✅ 测试通过！")
        print(f"工具执行成功没有抛出异常。")
        print(f"共返回了 {len(response.results)} 条结果。")
        print(f"实际执行的底层工具为: {response.tool_name}")
        
        if response.results:
            print("\n样板数据 (前两部):")
            for res in response.results[:2]:
                print(f" - [{res.platform}] {res.title_or_content[:50]}...")
                
    except Exception as e:
        print(f"\n❌ 测试失败！抛出了异常: {e}")

if __name__ == "__main__":
    test_fallback()
