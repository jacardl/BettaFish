import sys
from pathlib import Path
from loguru import logger

root = Path(r"d:\Users\BettaFish")
sys.path.append(str(root))

from InsightEngine.utils.data_ingestion import ingest_incremental_mediacrawler_data

if __name__ == "__main__":
    logger.info("=========================================")
    logger.info("Starting test for MediaCrawler...")
    try:
        count, details = ingest_incremental_mediacrawler_data("人工智能")
        logger.info(f"Test Finished. Result: {count} inserted, Details: {details}")
    except Exception as e:
        logger.error(f"Test Failed with exception: {e}")
    logger.info("=========================================")
