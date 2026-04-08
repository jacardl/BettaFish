import os
import json
from pathlib import Path
import sys

# 将项目根目录加入环境变量
sys.path.insert(0, str(Path(__file__).parent))

from InsightEngine.agent import DeepSearchAgent
from InsightEngine.state import State
from InsightEngine.utils.config import Settings
from loguru import logger

def main():
    # 1. 配置参数
    task_id = "20260407_1775576820055_96oa6"
    seed_id = "seed_1775490075893"
    
    # 明确指定你要覆盖的具体文件名
    report_dir = Path("insight_engine_streamlit_reports")
    state_file_name = "state_达巴水痕之地类魂_达巴水痕之地古格王朝_达巴水痕之地双主角__20260407_1775576820055_96oa6.json"
    report_file_name = "deep_search_report_达巴水痕之地类魂_达巴水痕之地古格王朝_达巴水痕之地双主角__20260407_1775576820055_96oa6.md"
    
    state_file = report_dir / state_file_name
    report_file = report_dir / report_file_name
    
    if not state_file.exists():
        logger.error(f"找不到状态文件: {state_file}")
        return

    logger.info(f"加载旧状态文件: {state_file}")
    
    # 2. 初始化 Agent
    config = Settings()
    agent = DeepSearchAgent(config=config)
    agent.load_state(str(state_file))
    
    # 强制将 seed_id 赋予 state，以便触发 Seed 解析
    agent.state.seed_id = seed_id
    agent.state.task_id = task_id
    
    # 3. 解析 Seed 文件，借用 agent 的逻辑将其转化为 seed_data_dict
    seed_path_txt = Path('final_reports/seeds') / f"{seed_id}.txt"
    seed_path_json = Path('output/seeds') / f"{seed_id}.json"
    
    if seed_path_json.exists():
        import json
        seed_data_dict = json.loads(seed_path_json.read_text(encoding='utf-8'))
        seed_context = seed_data_dict.get('text', '')
        agent._seed_data_dict = seed_data_dict
        logger.info(f"成功读取旧 Seed 文件(JSON): {seed_path_json}, 长度: {len(seed_context)}")
    elif seed_path_txt.exists():
        from datetime import datetime
        seed_context = seed_path_txt.read_text(encoding='utf-8')
        agent._seed_data_dict = {
            "text": seed_context,
            "filename": "用户上传的附件",
            "fake_url": f"seed://{seed_id}/attachment",
            "timestamp": datetime.now().isoformat()
        }
        logger.info(f"成功读取旧 Seed 文件(TXT): {seed_path_txt}, 长度: {len(seed_context)}")
    else:
        logger.error(f"找不到指定的 Seed 文件: {seed_path_txt}")
        return

    # 4. 执行注入逻辑
    # 我们不重新跑一遍网络爬虫，而是直接将 Seed 切片注入到第一段（paragraph[0]）的历史记录中，
    # 并重新执行第一段的 Summary (总结) 和后续的 Report Formatting
    
    if not agent.state.paragraphs:
        logger.error("State 中没有段落数据，无法注入！")
        return
        
    paragraph = agent.state.paragraphs[0]
    
    logger.info("开始将 Seed 文件内容注入到第一段事实信息源中...")
    seed_text = agent._seed_data_dict.get('text', '')
    seed_title = agent._seed_data_dict.get('filename', '用户上传的附件')
    seed_url = agent._seed_data_dict.get('fake_url', f"seed://attachment")
    
    # 切片
    seed_chunks = []
    paragraphs = [p.strip() for p in seed_text.split('\n') if p.strip()]
    current_chunk = []
    current_len = 0
    for p in paragraphs:
        current_chunk.append(p)
        current_len += len(p)
        if current_len > 800:
            seed_chunks.append("\n".join(current_chunk))
            current_chunk = []
            current_len = 0
    if current_chunk:
        seed_chunks.append("\n".join(current_chunk))
        
    if not seed_chunks:
        seed_chunks = [seed_text]

    # 转化为 Search 对象结构
    from InsightEngine.state import Search
    seed_searches = []
    from datetime import datetime
    for i, chunk in enumerate(seed_chunks):
        s = Search(
            query="提取用户附件信息",
            url=seed_url,
            title=f"【上传附件】{seed_title} (片段 {i+1}/{len(seed_chunks)})",
            content=chunk,
            score=100.0,
            timestamp=datetime.now().isoformat()
        )
        seed_searches.append(s)
    
    # 为了避免 Seed 文件因为上下文超长被截断，我们必须将其插入到搜索历史的最前面！
    # 先把之前可能重复注入的 seed 数据清理掉
    old_history = [s for s in agent.state.paragraphs[0].research.search_history if "seed://" not in s.url]
    
    agent.state.paragraphs[0].research.search_history = seed_searches + old_history
    logger.info(f"成功将 {len(seed_searches)} 个附件片段强制置顶到段落 0 的历史记录中。")
    
    # 5. 重写该段落的总结（让大模型重新阅读结合了 Seed 的结果）
    logger.info("重新生成包含 Seed 事实的段落总结...")
    
    # 获取所有的 search_results
    all_results_dict = []
    for s in agent.state.paragraphs[0].research.search_history:
        all_results_dict.append({
            "title": s.title,
            "content": s.content,
            "url": s.url
        })
        
    from InsightEngine.utils.text_processing import format_search_results_for_prompt
    
    # 强化 prompt，强制大模型优先阅读附件
    summary_input = {
        "title": paragraph.title,
        "content": "【重要指令】：必须优先、重点提取前面带【上传附件】的数据源中的观点和事实（如目标玩家分层、过热机制等），并融入到报告中。\n" + paragraph.content,
        "search_query": "综合分析包括附件在内的信息，附件内容具有最高优先级",
        "search_results": format_search_results_for_prompt(all_results_dict, config.MAX_CONTENT_LENGTH),
    }
    
    agent.state = agent.first_summary_node.mutate_state(summary_input, agent.state, 0)
    logger.info("第一段重写完成。")
    
    # 6. 重新生成最终的 Deep Search Report (Markdown)
    logger.info("重新拼接生成最终 Markdown 报告...")
    
    # 手动拼接报告数据，避免被大模型的熔断机制误杀（因为入参没有搜索结果字段）
    report_data = []
    for p in agent.state.paragraphs:
        report_data.append({
            "title": p.title,
            "paragraph_latest_state": p.research.latest_summary
        })
        
    final_report = agent.report_formatting_node.format_report_manually(report_data, agent.state.report_title)
    agent.state.final_report = final_report
    
    # 7. 保存文件覆盖旧文件
    # 直接手动将状态写入到指定的绝对路径文件，绕过 agent._save_report 内部的文件名生成逻辑
    state_file.write_text(agent.state.to_json(), encoding='utf-8')
    report_file.write_text(final_report, encoding='utf-8')
    
    logger.info(f"旧任务已成功修复并覆盖写入到:")
    logger.info(f" - {state_file}")
    logger.info(f" - {report_file}")

if __name__ == "__main__":
    main()
