import os
import logging
import yfinance as yf
import pandas as pd
from datetime import datetime, timezone

# Configure logging
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("YFDownloader")

def download_yf_data(symbol="TSLA"):
    logger.info(f"Downloading 60 days of 5m bars for {symbol} from yfinance (including extended hours)...")
    
    # Fetch data
    df = yf.download(
        tickers=symbol,
        period="60d",
        interval="5m",
        prepost=True,
        auto_adjust=False,
        progress=False
    )
    
    if df.empty:
        logger.error("No data returned from yfinance.")
        return

    # Handle MultiIndex columns if necessary
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = [col[0] for col in df.columns]
    
    # Normalize column names
    df.columns = [col.lower() for col in df.columns]
    
    # Filter for necessary columns
    required = ["open", "high", "low", "close", "volume"]
    df = df[required].copy()
    
    # Convert index to ISO strings for our NormalizedBar
    df.index = df.index.tz_convert("UTC").strftime("%Y%m%d  %H:%M:%S")
    
    # Add dummy columns for barCount and wap to match downloader format
    df["barCount"] = 0
    df["wap"] = df["close"]
    
    # Save to CSV
    os.makedirs("data", exist_ok=True)
    filename = f"data/{symbol}_5m_yf_60d.csv"
    df.to_csv(filename, index_label="date")
    
    logger.info(f"Successfully saved {len(df)} bars to {filename}")

if __name__ == "__main__":
    download_yf_data("TSLA")
