"""
章节篇幅规划节点。
"""

from __future__ import annotations

import json
from typing import Any, Dict, List

from loguru import logger

from ..core import TemplateSection
from ..prompts import (
    SYSTEM_PROMPT_WORD_BUDGET,
    build_word_budget_prompt,
)
from ..utils.json_parser import RobustJSONParser, JSONParseError
from .base_node import BaseNode


class WordBudgetNode(BaseNode):
    """
    规划各章节字数与重点。

    输出总字数、全局写作准则以及每章/小节的 target/min/max 字数约束。
    """

    def __init__(self, llm_client):
        """仅记录LLM客户端引用，方便run阶段发起请求"""
        super().__init__(llm_client, "WordBudgetNode")
        # 初始化鲁棒JSON解析器，启用所有修复策略
        self.json_parser = RobustJSONParser(
            enable_json_repair=True,
            enable_llm_repair=False,  # 可以根据需要启用LLM修复
            max_repair_attempts=3,
        )

    def run(
        self,
        sections: List[TemplateSection],
        design: Dict[str, Any],
        reports: Dict[str, str],
        forum_logs: str,
        query: str,
        template_overview: Dict[str, Any] | None = None,
    ) -> Dict[str, Any]:
        """
        根据设计稿和所有素材规划章节字数，让LLM写作时有明确篇幅目标。

        参数:
            sections: 模板章节列表。
            design: 布局节点返回的设计稿（title/toc/hero等）。
            reports: 三引擎报告映射。
            forum_logs: 论坛日志原文。
            query: 用户查询词。
            template_overview: 可选的模板概览，含章节元信息。

        返回:
            dict: 章节篇幅规划结果，包含 `totalWords`、`globalGuidelines` 与逐章 `chapters`。
        """
        # 截断过长的内容避免溢出
        truncated_reports = {}
        for k, v in reports.items():
            content = str(v)
            truncated_reports[k] = content[:15000] if len(content) > 15000 else content
            
        truncated_forum_logs = str(forum_logs)[:15000] if forum_logs else ""

        # 输入中除了章节骨架外，还包含布局节点输出，方便约束篇幅时参考视觉主次
        payload = {
            "query": query,
            "design": design,
            "sections": [section.to_dict() for section in sections],
            "templateOverview": template_overview
            or {
                "title": sections[0].title if sections else "",
                "chapters": [section.to_dict() for section in sections],
            },
            "reports": truncated_reports,
            "forumLogs": truncated_forum_logs,
        }
        user = build_word_budget_prompt(payload)
        response = self.llm_client.stream_invoke_to_string(
            SYSTEM_PROMPT_WORD_BUDGET,
            user,
            temperature=0.25,
            top_p=0.85,
        )
        plan = self._parse_response(response)
        
        # 强制后处理：移除任何在原始 template_overview 中不存在的非法新增小节（防幻觉扩展）
        if plan.get("chapters") and isinstance(plan["chapters"], list):
            valid_chapters = []
            for ch in plan["chapters"]:
                ch_title = ch.get("title", "")
                ch_id = ch.get("chapterId", "")
                
                # 寻找匹配的原始模板章节
                matched_tpl_sec = None
                for sec in sections:
                    if sec.slug == ch_id or sec.title == ch_title:
                        matched_tpl_sec = sec
                        break
                        
                if matched_tpl_sec and "sections" in ch and isinstance(ch["sections"], list):
                    import re
                    valid_sections = []
                    # 原始大纲的有效子标题前缀列表，例如 ["4.1"]
                    valid_prefixes = []
                    for out_item in matched_tpl_sec.outline:
                        if isinstance(out_item, dict):
                            out_title = out_item.get("title", "")
                        else:
                            out_title = str(out_item)
                        m = re.match(r"^([\d\.]+)", out_title.strip())
                        if m:
                            valid_prefixes.append(m.group(1))
                            
                    for sub_sec in ch["sections"]:
                        if isinstance(sub_sec, dict):
                            sub_title = sub_sec.get("title", "")
                        else:
                            sub_title = str(sub_sec)
                            sub_sec = {"title": sub_title}
                        # 如果子节标题的前缀不在 valid_prefixes 中，说明是 LLM 自己发明的（如 4.2）
                        m = re.match(r"^([\d\.]+)", sub_title.strip())
                        if m and valid_prefixes:
                            prefix = m.group(1)
                            # 如果前缀不是任何有效前缀的精确匹配，且前缀中有数字（如 4.2 不在 [4.1] 里）
                            if prefix not in valid_prefixes:
                                logger.warning(f"剔除越权生成的子章节: {sub_title}")
                                # 把非法章节的字数目标和重点合并到合法的最后一个章节里
                                if valid_sections:
                                    valid_sections[-1]["targetWords"] = valid_sections[-1].get("targetWords", 0) + sub_sec.get("targetWords", 0)
                                continue
                        valid_sections.append(sub_sec)
                    ch["sections"] = valid_sections
                    
            logger.info("章节字数规划已生成并完成越权剔除")
            
        return plan

    def _parse_response(self, raw: str) -> Dict[str, Any]:
        """
        将LLM输出的JSON文本转为字典，失败时提示规划异常。

        使用鲁棒JSON解析器进行多重修复尝试：
        1. 清理markdown标记和思考内容
        2. 本地语法修复（括号平衡、逗号补全、控制字符转义等）
        3. 使用json_repair库进行高级修复
        4. 可选的LLM辅助修复

        参数:
            raw: LLM返回值，可能包含```包裹、思考内容等。

        返回:
            dict: 合法的篇幅规划JSON。

        异常:
            ValueError: 当响应为空或JSON解析失败时抛出。
        """
        try:
            result = self.json_parser.parse(
                raw,
                context_name="篇幅规划",
                expected_keys=["totalWords", "globalGuidelines", "chapters"],
            )
            # 验证关键字段的类型
            if not isinstance(result.get("totalWords"), (int, float)):
                logger.warning("篇幅规划缺少totalWords字段或类型错误，使用默认值")
                result.setdefault("totalWords", 10000)
            if not isinstance(result.get("globalGuidelines"), list):
                logger.warning("篇幅规划缺少globalGuidelines字段或类型错误，使用空列表")
                result.setdefault("globalGuidelines", [])
            if not isinstance(result.get("chapters"), (list, dict)):
                logger.warning("篇幅规划缺少chapters字段或类型错误，使用空列表")
                result.setdefault("chapters", [])
            return result
        except JSONParseError as exc:
            # 转换为原有的异常类型以保持向后兼容
            raise ValueError(f"篇幅规划JSON解析失败: {exc}") from exc


__all__ = ["WordBudgetNode"]
