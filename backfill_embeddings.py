import asyncio
from InsightEngine.utils.db import fetch_all, execute_write
from utils.embedding import get_embedding

async def backfill():
    tables = [
        ("daily_news", "news_id", ["title", "description"]),
        ("xhs_note", "note_url", ["title", "\"desc\""]),
        ("bilibili_video", "video_url", ["title", "\"desc\""]),
        ("douyin_aweme", "aweme_url", ["title", "\"desc\""]),
        ("weibo_note", "note_url", ["content", "content"]),
        ("zhihu_content", "url", ["title", "content_text"]),
        ("tieba_note", "note_url", ["title", "\"desc\""])
    ]
    
    for tb_name, id_col, cols in tables:
        try:
            print(f"Backfilling {tb_name}...")
            # fetch rows where embedding is null
            rows = await fetch_all(f"SELECT {id_col}, {cols[0]} as col1, {cols[1]} as col2 FROM {tb_name} WHERE embedding IS NULL")
            print(f"Found {len(rows)} rows to backfill in {tb_name}")
            
            for r in rows:
                row_id = r[id_col]
                text = str(r['col1'] or '') + " " + str(r['col2'] or '')
                if not text.strip():
                    continue
                    
                emb = get_embedding(text)
                emb_str = f"[{','.join(map(str, emb))}]"
                
                await execute_write(f"UPDATE {tb_name} SET embedding = :emb WHERE {id_col} = :id", {"emb": emb_str, "id": row_id})
                
        except Exception as e:
            print(f"Error in {tb_name}: {e}")

if __name__ == "__main__":
    asyncio.run(backfill())
