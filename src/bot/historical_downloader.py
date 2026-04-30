import os
import sys
import time
import logging
import csv
from datetime import datetime, timedelta
import threading
from dotenv import load_dotenv

# Hack for broken ibapi protobuf imports in version 10.35.1
script_dir = os.path.dirname(os.path.abspath(__file__))
ibapi_path = os.path.join(script_dir, "ibapi")
protobuf_path = os.path.join(ibapi_path, "protobuf")
sys.path.insert(0, ibapi_path)
sys.path.insert(0, protobuf_path)

from core.ib_client import EventRouterClient
from ibapi.common import BarData

# Configure logging
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("HistoricalDownloader")

class HistoricalDownloader:
    def __init__(self, symbol="TSLA"):
        load_dotenv()
        self.host = os.getenv("TWS_HOST", "127.0.0.1")
        self.port = int(os.getenv("TWS_PORT", 4001))
        self.client_id = 99  # Separate ID for downloader
        self.symbol = symbol
        
        self.client = EventRouterClient()
        self.bars = []
        self.event = threading.Event()
        self.error_event = threading.Event()
        
        self.client.register_callback("historicalData", self.on_historical_data)
        self.client.register_callback("historicalDataEnd", self.on_historical_data_end)

    def on_historical_data(self, reqId: int, bar: BarData):
        self.bars.append({
            "date": bar.date,
            "open": bar.open,
            "high": bar.high,
            "low": bar.low,
            "close": bar.close,
            "volume": bar.volume,
            "barCount": bar.barCount,
            "wap": bar.wap
        })

    def on_historical_data_end(self, reqId: int, start: str, end: str):
        logger.info(f"Finished receiving chunk for reqId {reqId}")
        self.event.set()

    def download_6_months(self):
        logger.info(f"Connecting to IB at {self.host}:{self.port}...")
        self.client.connect(self.host, self.port, self.client_id)
        
        # Start client thread
        thread = threading.Thread(target=self.client.run, daemon=True)
        thread.start()
        
        # Wait for connection
        timeout = 10
        start_time = time.time()
        while not self.client.is_connected:
            if time.time() - start_time > timeout:
                logger.error("Timeout waiting for connection")
                return
            time.sleep(0.1)
            
        contract = EventRouterClient.get_contract(self.symbol)
        
        # 6 months is roughly 180 days. We'll download in 30-day chunks.
        all_bars = []
        # TWS reqHistoricalData endDateTime format: yyyymmdd hh:mm:ss {TMZ}
        # We start from today and go backwards
        end_date = datetime.now()
        
        for i in range(6):
            self.event.clear()
            end_date_str = end_date.strftime("%Y%m%d %H:%M:%S")
            logger.info(f"Requesting 30 days of data ending at {end_date_str}")
            
            # Duration: "30 D", BarSize: "5 mins", WhatToShow: "TRADES", UseRTH: 0 (Extended hours)
            self.client.reqHistoricalData(
                reqId=i,
                contract=contract,
                endDateTime=end_date_str,
                durationStr="30 D",
                barSizeSetting="5 mins",
                whatToShow="TRADES",
                useRTH=0,
                formatDate=1,
                keepUpToDate=False,
                chartOptions=[]
            )
            
            # Wait for this chunk to finish (with 60s timeout)
            if not self.event.wait(timeout=60):
                logger.warning(f"Timeout waiting for chunk {i}")
            
            all_bars.extend(self.bars)
            logger.info(f"Chunk {i} received {len(self.bars)} bars. Total: {len(all_bars)}")
            
            # Prep for next chunk
            if self.bars:
                # The first bar in the chunk is the oldest. Let's find its date.
                # Format: "20240101  09:30:00"
                first_bar_date_str = self.bars[0]["date"]
                try:
                    if " " in first_bar_date_str:
                        # Intra-day bar
                        dt = datetime.strptime(first_bar_date_str, "%Y%m%d  %H:%M:%S")
                    else:
                        # Daily bar (shouldn't happen with 5 min, but safe)
                        dt = datetime.strptime(first_bar_date_str, "%Y%m%d")
                    end_date = dt
                except Exception as e:
                    logger.error(f"Failed to parse date {first_bar_date_str}: {e}")
                    end_date -= timedelta(days=30)
            else:
                end_date -= timedelta(days=30)

            self.bars = []
            
            # Pacing violation prevention
            logger.info("Waiting 15 seconds to avoid pacing violation...")
            time.sleep(15)

        # Remove duplicates and sort
        unique_bars = {}
        for b in all_bars:
            unique_bars[b["date"]] = b
        
        sorted_dates = sorted(unique_bars.keys())
        final_bars = [unique_bars[d] for d in sorted_dates]
        
        logger.info(f"Download complete. Total unique bars: {len(final_bars)}")
        self.save_to_csv(final_bars)
        
        self.client.disconnect()

    def save_to_csv(self, bars):
        filename = f"data/{self.symbol}_5m_6m.csv"
        os.makedirs("data", exist_ok=True)
        keys = bars[0].keys() if bars else []
        with open(filename, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=keys)
            writer.writeheader()
            writer.writerows(bars)
        logger.info(f"Saved data to {filename}")

if __name__ == "__main__":
    downloader = HistoricalDownloader("TSLA")
    downloader.download_6_months()
