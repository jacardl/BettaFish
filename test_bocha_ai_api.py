import requests
import json

def test_bocha_ai_search():
    url = "https://api.bocha.cn/v1/ai-search"
    api_key = "sk-34a30b18692949658ca4624a20053e93"
    
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json"
    }
    
    payload = {
        "query": "《黑神话：悟空》销量突破多少了",
        "stream": False,
        "count": 5,
        "answer": True
    }
    
    print(f"Testing URL: {url}")
    print(f"With Key: {api_key}")
    print(f"Query: {payload['query']}")
    
    try:
        response = requests.post(url, headers=headers, json=payload, timeout=15)
        print(f"\nStatus Code: {response.status_code}")
        
        try:
            json_resp = response.json()
            print("\nResponse JSON Preview:")
            print(json.dumps(json_resp, indent=2, ensure_ascii=False)[:800] + "...")
        except Exception as parse_err:
            print(f"\nFailed to parse JSON: {parse_err}")
            print("Raw Response Text:")
            print(response.text[:800])
            
    except Exception as e:
        print(f"\nError: {str(e)}")

if __name__ == "__main__":
    test_bocha_ai_search()