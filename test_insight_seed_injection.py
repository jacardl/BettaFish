import os
import json
from pathlib import Path
from unittest.mock import MagicMock
import sys

# 将项目根目录加入环境变量，确保正常导入
sys.path.insert(0, str(Path(__file__).parent))

from InsightEngine.agent import DeepSearchAgent
from InsightEngine.state import State
from InsightEngine.utils.config import Settings

def test_seed_injection():
    print("=== 开始测试 Seed 注入功能 ===")
    
    # 1. 准备测试数据
    seed_id = "test_seed_888"
    root_dir = Path(__file__).parent
    seed_dir = root_dir / 'output' / 'seeds'
    seed_dir.mkdir(parents=True, exist_ok=True)
    seed_file_json = seed_dir / f"{seed_id}.json"
    
    # 构造一段长文本（故意超过 800 字，测试自动切片功能）
    chunk_1 = "这是第一段。商业计划书的核心要点。" * 50  # 约 850 字
    chunk_2 = "\n这是第二段。关于市场分析的详细数据。" * 50 # 约 850 字
    long_text = chunk_1 + chunk_2
    
    fake_seed_data = {
        "text": long_text,
        "filename": "测试企划书.pdf",
        "fake_url": f"seed://{seed_id}/测试企划书.pdf",
        "timestamp": "2026-04-08T12:00:00"
    }
    seed_file_json.write_text(json.dumps(fake_seed_data, ensure_ascii=False), encoding='utf-8')
    
    try:
        # 2. 初始化 Agent 并 Mock 外部依赖 (防止产生真实的 API/数据库 开销)
        config = Settings()
        agent = DeepSearchAgent(config=config)
        
        # 拦截 LLM 和 数据库 的真实调用
        agent.first_search_node = MagicMock()
        agent.first_search_node.run.return_value = {
            "search_query": "测试查询",
            "search_tool": "search_topic_globally",
            "reasoning": "Mock推理"
        }
        agent.first_summary_node = MagicMock()
        agent.first_summary_node.mutate_state = lambda inp, state, idx: state
        
        agent.search_agency = MagicMock()
        mock_db_response = MagicMock()
        mock_db_response.results = []
        agent.search_agency.search_topic_globally.return_value = mock_db_response
        
        # 3. 设置初始 State
        agent.state = State()
        agent.state.seed_id = seed_id
        agent.state.add_paragraph("第一章：测试注入", "预期内容")
        
        # 模拟 _generate_report_structure 中读取 seed 数据的逻辑
        agent._seed_data_dict = fake_seed_data
        
        # 4. 执行 _initial_search_and_summary (触发针对第 0 段的注入)
        print("\n-> 执行 _initial_search_and_summary(paragraph_index=0)...")
        agent._initial_search_and_summary(0)
        
        # 5. 验证结果
        paragraph = agent.state.paragraphs[0]
        searches = paragraph.research.search_history
        
        print(f"\n[验证结果]")
        print(f"提取出 {len(searches)} 个 Seed 结构化片段 (长文本已被成功切片)。")
        
        success = False
        for i, s in enumerate(searches):
            print(f"  片段 {i+1}:")
            print(f"    - 标题 (Title): {s.title}")
            print(f"    - URL: {s.url}")
            print(f"    - 文本长度: {len(s.content)} 字符")
            print(f"    - 相关度评分: {s.score}")
            if "测试企划书.pdf" in s.title and "seed://" in s.url and s.score == 100.0:
                success = True
                
        if success and len(searches) > 1:
            print("\n✅ 测试通过：Seed 文件已成功结构化、按长度分片，并注入到 Insight Engine 第一段的采集记录中！")
        else:
            print("\n❌ 测试失败：未能找到有效的 Seed 注入记录。")
            
    finally:
        # 清理测试产生的临时文件
        if seed_file_json.exists():
            seed_file_json.unlink()

if __name__ == "__main__":
    test_seed_injection()
