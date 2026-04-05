import asyncio
import os
import asyncpg
from dotenv import load_dotenv

async def test_db():
    load_dotenv()
    
    host = os.getenv("DB_HOST", "localhost")
    port = os.getenv("DB_PORT", "5444")
    user = os.getenv("DB_USER", "radar")
    password = os.getenv("DB_PASSWORD", "radar")
    database = os.getenv("DB_NAME", "radar")
    
    print(f"[*] 正在尝试连接数据库: postgresql://{user}:***@{host}:{port}/{database}")
    
    try:
        conn = await asyncpg.connect(
            host=host,
            port=port,
            user=user,
            password=password,
            database=database,
            timeout=5
        )
        print("[+] 数据库连接成功！✅")
        
        # 简单测试一下表是否存在
        tables = await conn.fetch("SELECT tablename FROM pg_catalog.pg_tables WHERE schemaname = 'public';")
        table_names = [record['tablename'] for record in tables]
        print(f"[*] 发现公共表: {table_names}")
        
        await conn.close()
        print("[+] 数据库测试完毕，连接正常关闭。")
        return True
    except Exception as e:
        print(f"[-] 数据库连接失败 ❌: {e}")
        return False

if __name__ == "__main__":
    success = asyncio.run(test_db())
    if not success:
        import sys
        sys.exit(1)
