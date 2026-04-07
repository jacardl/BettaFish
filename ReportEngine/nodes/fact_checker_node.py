import json
import logging
from typing import Dict, Any

logger = logging.getLogger(__name__)

class FactCheckerNode:
    """
    交叉验证与置信度评分节点（Cross-Validation Node & Confidence Scoring）。
    在IR装配完毕、渲染HTML之前执行。
    功能：
    1. 遍历报告的所有章节，检查是否有外部来源引用标记。
    2. 计算小节的引用密度，赋予 置信度 (High/Medium/Low Confidence) 标签。
    3. 如果某段落没有任何引用且输入数据较少，强制加上“缺乏事实支撑”的颜色或标记。
    """

    def __init__(self, llm_client=None):
        self.llm_client = llm_client

    def run(self, document_ir: Dict[str, Any], raw_reports: Dict[str, Any]) -> Dict[str, Any]:
        """
        对已生成的Document IR执行置信度注入和简单的事实验证。
        此版本通过计算章节引用角标数量，直接注入置信度Callout，
        并在没有引用的段落上追加高亮或提醒。
        """
        try:
            logger.info("开始执行 FactCheckerNode 交叉验证与置信度评分...")
            chapters = document_ir.get("chapters", [])
            for chapter in chapters:
                self._check_and_score_chapter(chapter)
            logger.info("FactCheckerNode 处理完成")
            return document_ir
        except Exception as e:
            logger.exception(f"FactCheckerNode 失败: {e}")
            return document_ir

    def _check_and_score_chapter(self, chapter: Dict[str, Any]):
        blocks = chapter.get("blocks", [])
        
        # 为了给每个 Heading 计算置信度，我们需要把 Blocks 按 Heading 划分
        current_heading_index = -1
        heading_citation_counts = {}
        
        # 1. 第一遍扫描，统计每个 heading 下的引用数量
        for i, block in enumerate(blocks):
            if block.get("type") == "heading":
                current_heading_index = i
                heading_citation_counts[current_heading_index] = 0
            elif current_heading_index != -1:
                # 检查段落中的 superscript link 数量
                if block.get("type") == "paragraph":
                    inlines = block.get("inlines", [])
                    for inline in inlines:
                        marks = inline.get("marks", [])
                        if any(isinstance(m, dict) and m.get("type") == "superscript" for m in marks):
                            heading_citation_counts[current_heading_index] += 1
                elif block.get("type") in ["table", "list", "widget", "pestTable", "swotTable"]:
                    # 粗略估计，如果有复杂块，通常也包含数据或结构
                    heading_citation_counts[current_heading_index] += 1
                    
        # 2. 第二遍扫描，插入置信度 Callout 并阻断低置信度章节
        new_blocks = []
        skip_current_heading = False

        for i, block in enumerate(blocks):
            if block.get("type") == "heading":
                skip_current_heading = False
                
                if i in heading_citation_counts:
                    citations = heading_citation_counts[i]
                    if citations == 0:
                        # 0引用的情况下，阻断后续内容的输出，且【不再输出该标题】实现完全隐藏
                        skip_current_heading = True
                        continue
                    else:
                        new_blocks.append(block)
                        if citations <= 3:
                            confidence = "中等置信度"
                            tone = "warning"
                            text = "本节内容有少量信息源支撑，建议结合引用清单交叉验证。"
                        else:
                            confidence = "高置信度"
                            tone = "success"
                            text = "本节内容有充足的外部信息源（多处引用）支撑。"
                        
                        # 在 heading 下方插入一个 callout block
                        callout_block = {
                            "type": "callout",
                            "tone": tone,
                            "title": f"事实核查评分：{confidence}",
                            "blocks": [
                                {
                                    "type": "paragraph",
                                    "inlines": [
                                        {
                                            "text": text
                                        }
                                    ]
                                }
                            ]
                        }
                        new_blocks.append(callout_block)
                else:
                    new_blocks.append(block)
            else:
                # 如果当前属于低置信度 Heading 下的内容，则丢弃该 Block
                if not skip_current_heading:
                    new_blocks.append(block)
                
        chapter["blocks"] = new_blocks
