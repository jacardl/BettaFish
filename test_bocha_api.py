import requests
import json

def test_bocha_web_search():
    url = "https://api.bocha.cn/v1/web-search"
    headers = {
        "Authorization": "Bearer sk-34a30b18692949658ca4624a20053e93",
        "Content-Type": "application/json"
    }
    payload = {
        "query": "人工智能"
    }
    
    print(f"Testing URL: {url}")
    print(f"With Key: sk-34a30b18692949658ca4624a20053e93")
    
    try:
        response = requests.post(url, headers=headers, json=payload, timeout=10)
        print(f"\nStatus Code: {response.status_code}")
        
        try:
            json_resp = response.json()
            print("\nResponse JSON:")
            print(json.dumps(json_resp, indent=2, ensure_ascii=False)[:500] + "...")
        except:
            print("\nRaw Response Text:")
            print(response.text[:500])
            
    except Exception as e:
        print(f"\nError: {str(e)}")

if __name__ == "__main__":
    test_bocha_web_search()