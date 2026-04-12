import sys
from pathlib import Path
from loguru import logger
import time

root = Path(r"d:\Users\BettaFish")
sys.path.append(str(root))

from InsightEngine.utils.data_ingestion import ingest_incremental_mediacrawler_data

if __name__ == "__main__":
    logger.info("=========================================")
    logger.info("Starting MediaCrawler for 《达巴：水痕之地》...")
    logger.info("将依次启动 抖音/小红书/B站/知乎/微博 平台抓取。")
    logger.info("由于您需要固化登录信息，程序将依次打开这些平台的网页。")
    logger.info("【请注意】：当浏览器弹窗出现时，请务必使用手机对应APP扫描弹出的二维码登录！")
    logger.info("登录成功后，状态将自动保存，后续即可持续使用。")
    logger.info("=========================================")
    
    # 延迟 3 秒，让用户有心理准备看日志
    time.sleep(3)

    platforms = ['dy', 'xhs', 'bili', 'zhihu', 'wb']
    
    try:
        count, details = ingest_incremental_mediacrawler_data("游戏《达巴：水痕之地》", platforms=platforms)
        logger.info(f"Test Finished. Result: {count} inserted, Details: {details}")
    except Exception as e:
        logger.error(f"Test Failed with exception: {e}")
    logger.info("=========================================")
