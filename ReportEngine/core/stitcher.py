"""
章节装订器：负责把多个章节JSON合并为整本IR。

DocumentComposer 会注入缺失锚点、统一顺序，并补齐 IR 级元数据。
"""

from __future__ import annotations

from datetime import datetime
from typing import Dict, List, Set

from ..ir import IR_VERSION


class DocumentComposer:
    """
    将章节拼接成Document IR的简单装订器。

    作用：
        - 按order排序章节，补充默认chapterId；
        - 防止anchor重复，生成全局唯一锚点；
        - 注入 IR 版本与生成时间戳。
    """

    def __init__(self):
        """初始化装订器并记录已使用的锚点，避免重复"""
        self._seen_anchors: Set[str] = set()

    def build_document(
        self,
        report_id: str,
        metadata: Dict[str, object],
        chapters: List[Dict[str, object]],
    ) -> Dict[str, object]:
        """
        把所有章节按order排序并注入唯一锚点，形成整本IR。

        同时合并 metadata/themeTokens/assets，供渲染器直接消费。

        参数:
            report_id: 本次报告ID。
            metadata: 全局元信息（标题、主题、toc等）。
            chapters: 章节payload列表。

        返回:
            dict: 满足渲染器需求的Document IR。
        """
        # 全局处理合并所有的引用文献，并重新编号
        self._consolidate_citations(chapters)

        # 构建从chapterId到toc anchor的映射
        toc_anchor_map = self._build_toc_anchor_map(metadata)

        ordered = sorted(chapters, key=lambda c: c.get("order", 0))
        for idx, chapter in enumerate(ordered, start=1):
            chapter.setdefault("chapterId", f"S{idx}")

            # 优先级：1. 目录配置的anchor 2. 章节自带的anchor 3. 默认anchor
            chapter_id = chapter.get("chapterId")
            anchor = (
                toc_anchor_map.get(chapter_id) or
                chapter.get("anchor") or
                f"section-{idx}"
            )
            chapter["anchor"] = self._ensure_unique_anchor(anchor)
            chapter.setdefault("order", idx * 10)
            if chapter.get("errorPlaceholder"):
                self._ensure_heading_block(chapter)

        document = {
            "version": IR_VERSION,
            "reportId": report_id,
            "metadata": {
                **metadata,
                "generatedAt": metadata.get("generatedAt")
                or datetime.utcnow().isoformat() + "Z",
            },
            "themeTokens": metadata.get("themeTokens", {}),
            "chapters": ordered,
            "assets": metadata.get("assets", {}),
        }
        return document

    def _ensure_unique_anchor(self, anchor: str) -> str:
        """若存在重复锚点则追加序号，确保全局唯一。"""
        base = anchor
        counter = 2
        while anchor in self._seen_anchors:
            anchor = f"{base}-{counter}"
            counter += 1
        self._seen_anchors.add(anchor)
        return anchor

    def _build_toc_anchor_map(self, metadata: Dict[str, object]) -> Dict[str, str]:
        """
        从metadata.toc.customEntries构建chapterId到anchor的映射。
        """
        toc_config = metadata.get("toc") or {}
        custom_entries = toc_config.get("customEntries") or []
        anchor_map = {}

        for entry in custom_entries:
            if isinstance(entry, dict):
                chapter_id = entry.get("chapterId")
                anchor = entry.get("anchor")
                if chapter_id and anchor:
                    anchor_map[chapter_id] = anchor

        return anchor_map

    def _consolidate_citations(self, chapters: List[Dict[str, object]]) -> None:
        """
        把所有章节中的 citationList 合并为一份全局参考资料，并修正正文中的 inline citation 编号。
        """
        global_citations = []
        url_to_global_index = {}
        title_to_global_index = {}
        global_index_counter = 1
        
        chapter_index_maps = []

        for chapter in chapters:
            blocks = chapter.get("blocks", [])
            if not isinstance(blocks, list):
                chapter_index_maps.append({})
                continue
                
            local_map = {}
            new_blocks = []
            
            for block in blocks:
                if isinstance(block, dict) and block.get("type") == "citationList":
                    items = block.get("items", [])
                    for item in items:
                        url = item.get("url", "").strip()
                        title = item.get("title", "").strip()
                        local_idx = str(item.get("index", ""))
                        
                        if url and url in url_to_global_index:
                            g_idx = url_to_global_index[url]
                        elif not url and title and title in title_to_global_index:
                            g_idx = title_to_global_index[title]
                        else:
                            g_idx = global_index_counter
                            global_index_counter += 1
                            new_item = dict(item)
                            new_item["index"] = g_idx
                            global_citations.append(new_item)
                            
                            if url:
                                url_to_global_index[url] = g_idx
                            if title:
                                title_to_global_index[title] = g_idx
                                
                        if local_idx:
                            local_map[local_idx] = g_idx
                else:
                    new_blocks.append(block)
            
            chapter["blocks"] = new_blocks
            chapter_index_maps.append(local_map)
            
        import re
        
        def walk_and_replace_inlines(node, local_map):
            if isinstance(node, dict):
                if "inlines" in node and isinstance(node["inlines"], list):
                    new_inlines = []
                    for inline in node["inlines"]:
                        if not isinstance(inline, dict):
                            new_inlines.append(inline)
                            continue
                        
                        marks = inline.get("marks", [])
                        is_citation = False
                        href = ""
                        for m in marks:
                            if isinstance(m, dict) and m.get("type") == "link":
                                link_href = str(m.get("href", ""))
                                if link_href.startswith("#citation-"):
                                    is_citation = True
                                    href = link_href
                                    break
                        
                        if is_citation:
                            match = re.search(r'#citation-(\d+)', href)
                            if match:
                                local_idx = match.group(1)
                                if local_idx in local_map:
                                    g_idx = local_map[local_idx]
                                    
                                    actual_url = ""
                                    for gc in global_citations:
                                        if gc["index"] == g_idx:
                                            actual_url = gc.get("url", "")
                                            break
                                            
                                    inline_text = str(inline.get("text", ""))
                                    
                                    # 针对表格中长文本引用的优化：如果 text 很长（包含描述），则直接将 href 替换为真实 url
                                    # 否则只替换编号并指向文末
                                    if len(inline_text) > 5 and actual_url:
                                        if f"[{local_idx}]" in inline_text:
                                            inline["text"] = inline_text.replace(f"[{local_idx}]", f"[{g_idx}]")
                                        for m in marks:
                                            if isinstance(m, dict) and m.get("type") == "link":
                                                m["href"] = actual_url
                                    else:
                                        inline["text"] = f"[{g_idx}]"
                                        for m in marks:
                                            if isinstance(m, dict) and m.get("type") == "link":
                                                m["href"] = f"#citation-{g_idx}"
                                    new_inlines.append(inline)
                                else:
                                    # 如果在 citationList 中找不到该编号，说明是 LLM 幻觉编造的越界引用，直接丢弃该标记
                                    pass
                            else:
                                new_inlines.append(inline)
                        else:
                            new_inlines.append(inline)
                    node["inlines"] = new_inlines
                
                for k, v in node.items():
                    if k != "inlines":
                        walk_and_replace_inlines(v, local_map)
            elif isinstance(node, list):
                for item in node:
                    walk_and_replace_inlines(item, local_map)

        for chapter, local_map in zip(chapters, chapter_index_maps):
            walk_and_replace_inlines(chapter.get("blocks"), local_map)
            
        if global_citations and chapters:
            last_chapter = chapters[-1]
            last_chapter["blocks"].append({
                "type": "citationList",
                "items": global_citations
            })

    def _ensure_heading_block(self, chapter: Dict[str, object]) -> None:
        """保证占位章节仍然拥有可用于目录的heading block。"""
        blocks = chapter.get("blocks")
        if isinstance(blocks, list):
            for block in blocks:
                if isinstance(block, dict) and block.get("type") == "heading":
                    return
        heading = {
            "type": "heading",
            "level": 2,
            "text": chapter.get("title") or "占位章节",
            "anchor": chapter.get("anchor"),
        }
        if isinstance(blocks, list):
            blocks.insert(0, heading)
        else:
            chapter["blocks"] = [heading]


__all__ = ["DocumentComposer"]
