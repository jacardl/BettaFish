import json
from InsightEngine.utils.text_processing import fix_incomplete_json

def test_json_repair():
    # 测试大模型输出截断的场景
    truncated_json = '{\n  "updated_paragraph_latest_state": "### 核心发现\\n《达巴：水痕之地》是一款由独立工作室开发的游戏，包含了丰富的文化。'
    
    print("原始截断 JSON:")
    print(truncated_json)
    print("-" * 50)
    
    repaired = fix_incomplete_json(truncated_json)
    print("修复后的 JSON:")
    print(repaired)
    
    try:
        data = json.loads(repaired)
        print("-" * 50)
        print("✅ 解析成功！提取的内容为:")
        print(data.get("updated_paragraph_latest_state"))
    except Exception as e:
        print("❌ 解析失败:", e)

if __name__ == "__main__":
    test_json_repair()