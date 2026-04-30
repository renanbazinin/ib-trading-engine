import pandas as pd
import pandas_ta as ta
import logging

logger = logging.getLogger(__name__)


def _as_float(value, default=0.0):
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _as_int(value, default=0):
    try:
        return int(value)
    except (TypeError, ValueError):
        return default

class BBSmiStrategy:
    """
    A standalone logic class implementing Bollinger Bands and 
    Stochastic Momentum Index crossover conditions.
    """
    def __init__(self, bb_length=20, bb_std=2, smi_fast=10, smi_slow=3, smi_sig=3, near_band_pct=0.001):
        self.base_columns = ['open', 'high', 'low', 'close', 'volume', 'barCount', 'wap']
        self.df = pd.DataFrame(columns=self.base_columns)

        self.bb_length = bb_length
        self.bb_std = bb_std
        self.smi_fast = smi_fast
        self.smi_slow = smi_slow
        self.smi_sig = smi_sig
        self.near_band_pct = near_band_pct

    @property
    def min_bars_required(self) -> int:
        return self.bb_length + self.smi_fast + self.smi_slow

    def add_bar(self, date_str, open_price, high, low, close, volume, barCount=0, wap=0.0):
        """Append a new bar or update an existing one"""
        try:
            # yfinance index format "20240101  09:30:00" or similar
            dt = pd.to_datetime(date_str)
        except Exception:
            dt = date_str

        # Only update core bar columns so indicator columns remain intact.
        self.df.at[dt, 'open'] = _as_float(open_price)
        self.df.at[dt, 'high'] = _as_float(high)
        self.df.at[dt, 'low'] = _as_float(low)
        self.df.at[dt, 'close'] = _as_float(close)
        self.df.at[dt, 'volume'] = _as_float(volume)
        self.df.at[dt, 'barCount'] = _as_int(barCount)
        self.df.at[dt, 'wap'] = _as_float(wap)

    def update_indicators(self):
        close_series = pd.to_numeric(self.df['close'], errors='coerce')
        
        if len(self.df) % 1000 == 0:
             logger.debug(f"Strategy DF size: {len(self.df)}")

        if close_series.count() < self.bb_length:
            return
            
        bbands = ta.bbands(close_series, length=self.bb_length, std=self.bb_std)
        if bbands is not None:
            self.df['BB_LOWER'] = bbands.iloc[:, 0]
            self.df['BB_MID'] = bbands.iloc[:, 1]
            self.df['BB_UPPER'] = bbands.iloc[:, 2]

        smi_df = ta.smi(close_series, fast=self.smi_fast, slow=self.smi_slow, signal=self.smi_sig)
        if smi_df is not None:
            self.df['SMI'] = smi_df.iloc[:, 0]
            self.df['SMI_SIGNAL'] = smi_df.iloc[:, 1]
            
    def get_latest_signal(self):
        """
        Returns BUY, SELL, or HOLD.
        """
        if 'BB_LOWER' not in self.df.columns or 'SMI' not in self.df.columns:
            return "HOLD", None
            
        if len(self.df) < 2:
            return "HOLD", None

        latest = self.df.iloc[-1]
        previous = self.df.iloc[-2]
        
        if pd.isna(latest['SMI']) or pd.isna(latest['BB_LOWER']):
            return "HOLD", None
            
        # Crossings
        cross_up = previous['SMI'] < previous['SMI_SIGNAL'] and latest['SMI'] > latest['SMI_SIGNAL']
        cross_down = previous['SMI'] > previous['SMI_SIGNAL'] and latest['SMI'] < latest['SMI_SIGNAL']
        
        # Thresholds
        buy_threshold = latest['BB_LOWER'] * (1 + self.near_band_pct)
        sell_threshold = latest['BB_UPPER'] * (1 - self.near_band_pct)
        
        if cross_up:
            logger.debug(f"SMI Cross UP at {latest['close']}. BB_LOWER: {latest['BB_LOWER']}. Threshold: {buy_threshold}")
            if latest['close'] <= buy_threshold:
                logger.info(f"!!! BUY SIGNAL !!! at {latest['close']}")
                return "BUY", latest['close']
            
        if cross_down:
            logger.debug(f"SMI Cross DOWN at {latest['close']}. BB_UPPER: {latest['BB_UPPER']}. Threshold: {sell_threshold}")
            if latest['close'] >= sell_threshold:
                logger.info(f"!!! SELL SIGNAL !!! at {latest['close']}")
                return "SELL", latest['close']
            
        return "HOLD", latest['close']
