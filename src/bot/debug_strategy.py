import pandas as pd
import pandas_ta as ta
import os

def debug_tsla():
    csv_path = "data/TSLA_5m_yf_60d.csv"
    if not os.path.exists(csv_path):
        print("CSV not found")
        return

    df = pd.read_csv(csv_path)
    
    # Strategy parameters
    length = 20
    std = 2.0
    smi_fast = 10
    smi_slow = 3
    smi_sig = 3
    near_band_pct = 0.005 # 0.5% - much looser

    # Compute Indicators
    close_series = pd.to_numeric(df['close'], errors='coerce')
    bbands = ta.bbands(close_series, length=length, std=std)
    df['BB_LOWER'] = bbands.iloc[:, 0]
    df['BB_UPPER'] = bbands.iloc[:, 2]
    
    smi_df = ta.smi(close_series, fast=smi_fast, slow=smi_slow, signal=smi_sig)
    df['SMI'] = smi_df.iloc[:, 0]
    df['SMI_SIGNAL'] = smi_df.iloc[:, 1]
    
    # Check conditions
    # Crosses
    df['SMI_CROSS_UP'] = (df['SMI'].shift(1) < df['SMI_SIGNAL'].shift(1)) & (df['SMI'] > df['SMI_SIGNAL'])
    df['SMI_CROSS_DOWN'] = (df['SMI'].shift(1) > df['SMI_SIGNAL'].shift(1)) & (df['SMI'] < df['SMI_SIGNAL'])
    
    # Near Price touch
    df['NEAR_LOWER'] = df['close'] <= (df['BB_LOWER'] * (1 + near_band_pct))
    df['NEAR_UPPER'] = df['close'] >= (df['BB_UPPER'] * (1 - near_band_pct))
    
    # Combined
    df['BUY_SIGNAL'] = df['NEAR_LOWER'] & df['SMI_CROSS_UP']
    df['SELL_SIGNAL'] = df['NEAR_UPPER'] & df['SMI_CROSS_DOWN']
    
    print(f"Loaded {len(df)} bars")
    print(f"SMI Cross Up: {df['SMI_CROSS_UP'].sum()}")
    print(f"SMI Cross Down: {df['SMI_CROSS_DOWN'].sum()}")
    print(f"Near Lower BB (0.5%): {df['NEAR_LOWER'].sum()}")
    print(f"Near Upper BB (0.5%): {df['NEAR_UPPER'].sum()}")
    print(f"BUY SIGNALS: {df['BUY_SIGNAL'].sum()}")
    print(f"SELL SIGNALS: {df['SELL_SIGNAL'].sum()}")

if __name__ == "__main__":
    debug_tsla()
