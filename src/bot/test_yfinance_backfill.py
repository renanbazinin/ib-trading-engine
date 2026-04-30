"""Standalone test: verify what yfinance returns for backfill.
Run: python test_yfinance_backfill.py
Outputs: test_backfill_results.json
"""
import json
import yfinance as yf
import pandas as pd
from datetime import datetime, timezone

SYMBOL = "TSLA"
INTERVAL = "5m"
PERIOD = "5d"

results = {}

for prepost_val in [False, True]:
    label = f"prepost={prepost_val}"
    print(f"\n--- Downloading {SYMBOL} period={PERIOD} interval={INTERVAL} {label} ---")

    try:
        df = yf.download(
            tickers=SYMBOL,
            period=PERIOD,
            interval=INTERVAL,
            auto_adjust=False,
            prepost=prepost_val,
            progress=False,
            threads=False,
            group_by="column",
        )

        if df is None or df.empty:
            results[label] = {"status": "EMPTY", "rows": 0}
            print(f"  Result: EMPTY")
            continue

        if isinstance(df.columns, pd.MultiIndex):
            if SYMBOL in df.columns.get_level_values(0):
                df = df[SYMBOL]
            elif SYMBOL in df.columns.get_level_values(-1):
                df = df.xs(SYMBOL, axis=1, level=-1, drop_level=True)
            else:
                df.columns = [col[0] for col in df.columns]

        df.columns = [str(c).strip().lower() for c in df.columns]

        index_utc = pd.to_datetime(df.index, utc=True, errors="coerce")
        valid = ~index_utc.isna()
        df = df.loc[valid].copy()
        df.index = index_utc[valid]
        df = df.dropna(subset=["open", "high", "low", "close"])

        now = datetime.now(timezone.utc)
        newest_ts = df.index[-1] if len(df) > 0 else None
        oldest_ts = df.index[0] if len(df) > 0 else None
        lag = (now - newest_ts).total_seconds() if newest_ts else None

        sample_bars = []
        show_indices = list(range(min(3, len(df)))) + list(range(max(0, len(df) - 3), len(df)))
        for i in sorted(set(show_indices)):
            row = df.iloc[i]
            sample_bars.append({
                "index": i,
                "time": str(df.index[i]),
                "open": float(row["open"]),
                "close": float(row["close"]),
                "volume": float(row.get("volume", 0)),
            })

        entry = {
            "status": "OK",
            "rows": len(df),
            "oldest": str(oldest_ts),
            "newest": str(newest_ts),
            "newest_lag_seconds": round(lag) if lag else None,
            "columns": list(df.columns),
            "sample_bars": sample_bars,
        }
        results[label] = entry
        print(f"  Result: {len(df)} bars, oldest={oldest_ts}, newest={newest_ts}, lag={lag:.0f}s")

    except Exception as exc:
        results[label] = {"status": "ERROR", "error": str(exc)}
        print(f"  Error: {exc}")

output_path = "test_backfill_results.json"
with open(output_path, "w") as f:
    json.dump(results, f, indent=2, default=str)

print(f"\nResults written to {output_path}")
print(json.dumps(results, indent=2, default=str))
