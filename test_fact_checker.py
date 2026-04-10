from ReportEngine.nodes.fact_checker_node import FactCheckerNode
import json

def test_fact_checker():
    # 模拟一个没有引用的章节
    chapter = {
        "chapterId": "4.1",
        "title": "4.1 竞品介绍",
        "blocks": [
            {
                "type": "heading",
                "level": 2,
                "text": "4.1 竞品介绍"
            },
            {
                "type": "paragraph",
                "inlines": [
                    {
                        "text": "有传言说《达巴》开发团队曾接触过某知名发行商，但这并没有其他可靠的信息源。"
                    }
                ]
            }
        ]
    }
    
    document_ir = {"chapters": [chapter]}
    node = FactCheckerNode()
    new_ir = node.run(document_ir, {})
    
    print(json.dumps(new_ir["chapters"][0], indent=2, ensure_ascii=False))

if __name__ == "__main__":
    test_fact_checker()