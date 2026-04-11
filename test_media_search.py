import asyncio
import os
import sys

os.environ["HF_ENDPOINT"] = "https://hf-mirror.com"

# Add project root to sys.path to allow imports
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from MediaEngine.tools.search import BochaMultimodalSearch

async def test_media_search():
    print("Testing BochaMultimodalSearch (Local DB)...")
    search_tool = BochaMultimodalSearch()
    
    # Test comprehensive search
    print("\n--- Test comprehensive_search ---")
    res = search_tool.comprehensive_search("硬核", max_results=3)
    print(f"Results: {len(res.webpages)}")
    for i, w in enumerate(res.webpages):
        print(f"[{i+1}] {w.name} - {w.url}")

if __name__ == "__main__":
    asyncio.run(test_media_search())
