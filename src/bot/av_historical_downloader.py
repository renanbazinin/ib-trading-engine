import os
import time
import logging
import requests
import pandas as pd
from io import StringIO
from dotenv import load_dotenv

# Configure logging
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("AVDownloader")

def download_av_6m_data(symbol="TSLA"):
    load_dotenv()
    api_key = os.getenv("ALPHA_VANTAGE_API_KEY", "demo")
    
    if api_key == "demo" or not api_key:
        logger.warning("No API key found in .env (or using 'demo'). Free 'demo' key only works for IBM.")
    
    logger.info(f"Downloading 6 months of 5m bars for {symbol} from Alpha Vantage...")
    
    # Alpha Vantage Extended Intraday uses 'yearXmonthY' slices (e.g., year1month1 is most recent)
    # We need slices for the last 6 months.
    all_dfs = []
    
    # Year 1 (last 12 months)
    # month1 is most recent, month6 is 6 months ago
    for m in range(1, 7):
        slice_name = f"year1month{m}"
        logger.info(f"Fetching slice: {slice_name}")
        
        url = f"https://www.alphavantage.co/query?function=TIME_SERIES_INTRADAY_EXTENDED&symbol={symbol}&interval=5min&slice={slice_name}&apikey={api_key}"
        
        try:
            response = requests.get(url)
            if response.status_code != 200:
                logger.error(f"Error {response.status_code} fetching slice {slice_name}")
                continue
                
            if "Note" in response.text and "API call frequency" in response.text:
                logger.warning("Rate limit hit. Waiting 65 seconds...")
                time.sleep(65)
                # Retry once
                response = requests.get(url)

            df = pd.read_csv(StringIO(response.text))
            
            if df.empty or "time" not in df.columns:
                logger.error(f"Slice {slice_name} returned invalid data: {response.text[:100]}")
                continue
                
            logger.info(f"Received {len(df)} rows for {slice_name}")
            all_dfs.append(df)
            
            # Rate limit safety for free tier (5 calls per minute)
            if m < 6:
                logger.info("Waiting 15 seconds to respect rate limits...")
                time.sleep(15)
                
        except Exception as e:
            logger.error(f"Failed to fetch slice {slice_name}: {e}")

    if not all_dfs:
        logger.error("No data fetched.")
        return

    # Combine and sort
    final_df = pd.concat(all_dfs, ignore_index=True)
    final_df = final_df.drop_duplicates(subset=["time"])
    
    # AV returns newest first, we want oldest first for simulations
    final_df['time'] = pd.to_datetime(final_df['time'])
    final_df = final_df.sort_values(by="time", ascending=True)
    
    # Map to our NormalizedBar format
    # AV columns: time,open,high,low,close,volume
    final_df.rename(columns={
        "time": "date",
    }, inplace=True)
    
    # Format date to match our CSV convention: "20240101  09:30:00"
    final_df["date"] = final_df["date"].dt.strftime("%Y%m%d  %H:%M:%S")
    final_df["barCount"] = 0
    final_df["wap"] = final_df["close"]
    
    # Save to CSV
    os.makedirs("data", exist_ok=True)
    filename = f"data/{symbol}_5m_6m_av.csv"
    final_df.to_csv(filename, index=False)
    
    logger.info(f"Successfully saved {len(final_df)} bars to {filename}")
    return filename

if __name__ == "__main__":
    download_av_6m_data("TSLA")
