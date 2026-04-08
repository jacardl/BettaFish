import requests
import json
import os
from dotenv import load_dotenv

# 加载环境变量
load_dotenv()
api_key = os.getenv("ANSPIRE_API_KEY")

def test_anspire_api(url_name, url, is_post=False):
    print(f"\n{'='*50}")
    print(f"Testing {url_name} API: {url}")
    print(f"{'='*50}")
    
    if not api_key:
        print("Error: ANSPIRE_API_KEY not found in .env file")
        return
        
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json"
    }
    
    query = "黑神话悟空 销量"
    
    try:
        if is_post:
            payload = {
                "query": query,
                "top_k": 1,
                "detail": True # pro版通常需要这个参数获取正文
            }
            response = requests.post(url, headers=headers, json=payload, timeout=15)
        else:
            params = {
                "query": query,
                "top_k": 1,
                "detail": True
            }
            response = requests.get(url, headers=headers, params=params, timeout=15)
            
        print(f"Status Code: {response.status_code}")
        
        try:
            json_resp = response.json()
            
            # 提取第一个结果的信息，以比较内容长度
            results = json_resp.get("results", []) if "results" in json_resp else json_resp.get("data", [])
            
            if results and len(results) > 0:
                first_result = results[0]
                
                print("\n[第一条搜索结果摘要]")
                print(f"标题: {first_result.get('title', 'N/A')}")
                print(f"URL: {first_result.get('url', 'N/A')}")
                
                content = first_result.get('content', '')
                raw_content = first_result.get('raw_content', '') or first_result.get('detail', '') or first_result.get('markdown', '')
                
                print(f"\n-> Content (摘要) 长度: {len(content)} 字符")
                print(f"预览: {content[:150]}...")
                
                if raw_content:
                    print(f"\n-> Raw Content (正文) 长度: {len(raw_content)} 字符")
                    print(f"预览: {raw_content[:150]}...")
                else:
                    print("\n-> Raw Content (正文): [未返回该字段或为空]")
                    
                print("\n[完整返回结构预览]")
                # 隐藏长文本，只看结构
                preview_dict = json_resp.copy()
                if "results" in preview_dict and preview_dict["results"]:
                    for res in preview_dict["results"]:
                        if "content" in res: res["content"] = f"...[{len(res['content'])} chars]..."
                        if "raw_content" in res: res["raw_content"] = f"...[{len(res['raw_content'])} chars]..."
                        if "detail" in res: res["detail"] = f"...[{len(res['detail'])} chars]..."
                print(json.dumps(preview_dict, indent=2, ensure_ascii=False)[:800])
            else:
                print("No results found.")
                print(json.dumps(json_resp, indent=2, ensure_ascii=False)[:500])
                
        except Exception as parse_err:
            print(f"Failed to parse JSON: {parse_err}")
            print(f"Raw text: {response.text[:500]}")
            
    except Exception as e:
        print(f"Error: {str(e)}")

if __name__ == "__main__":
    # 测试标准版 (GET请求)
    test_anspire_api("Standard", "https://plugin.anspire.cn/api/ntsearch/search", is_post=False)
    
    # 测试 Pro 版 (通常是POST请求)
    test_anspire_api("Pro", "https://plugin.anspire.cn/api/ntsearch/prosearch", is_post=False)