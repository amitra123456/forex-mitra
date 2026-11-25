"""
MetaTrader 5 Real-Time & Historical Data Feed Adapter (ANT_FOREX)
Connects directly to the local MT5 terminal with zero latency.
"""

from datetime import datetime, timezone
from typing import Optional, Union
from zoneinfo import ZoneInfo
import MetaTrader5 as mt5
import numpy as np
import pandas as pd

NY_TZ = ZoneInfo("America/New_York")


def mt5_server_ts_to_utc_series(ts_seconds: Union[pd.Series, np.ndarray]) -> pd.Series:
    """
    Deterministically converts MT5 Broker Server timestamps (EET/EEST = America/New_York + 7h,
    so 17:00 NY close = 00:00 Broker Time year-round: UTC+2 in US winter, UTC+3 in US summer)
    into timezone-aware True UTC timestamps, without drifting when market ticks pause.
    """
    s = pd.to_datetime(pd.Series(ts_seconds), unit="s") - pd.Timedelta(hours=7)
    aware_ny = s.dt.tz_localize(NY_TZ, ambiguous="NaT", nonexistent="shift_forward")
    # For the rare 1-hour fall-back ambiguous window on Sunday night, fallback to EST (-5h -> +2h broker)
    if aware_ny.isna().any():
        fallback_utc = pd.to_datetime(pd.Series(ts_seconds) - 7200, unit="s", utc=True)
        return aware_ny.dt.tz_convert("UTC").fillna(fallback_utc)
    return aware_ny.dt.tz_convert("UTC")


def mt5_ts_to_utc_epoch(mt5_ts: float) -> float:
    """Converts a single MT5 server timestamp (e.g. pos.time or tick.time) to True UTC Unix epoch seconds."""
    naive_ny = datetime.fromtimestamp(float(mt5_ts), tz=timezone.utc).replace(tzinfo=None) - pd.Timedelta(hours=7)
    dt_ny = naive_ny.replace(tzinfo=NY_TZ)
    return float(dt_ny.astimezone(timezone.utc).timestamp())


def get_position_elapsed_seconds(pos_or_ts, now_utc_ts: Optional[float] = None) -> float:
    """Returns true elapsed seconds since an MT5 position opened, correcting MT5 UTC+2/+3 server time."""
    raw_ts = float(pos_or_ts.time) if hasattr(pos_or_ts, "time") else float(pos_or_ts)
    open_utc_ts = mt5_ts_to_utc_epoch(raw_ts)
    ref_ts = float(now_utc_ts) if now_utc_ts is not None else datetime.now(timezone.utc).timestamp()
    return max(0.0, ref_ts - open_utc_ts)


MT5_TERMINAL_PATH = r"C:\Program Files\MetaTrader 5\terminal64.exe"


def initialize_mt5() -> bool:
    """Initialize connection to local MetaTrader 5 terminal (explicit path avoids binding to liveupdate helper)."""
    import os
    try:
        info = mt5.terminal_info()
        if info is not None and getattr(info, "connected", False):
            return True
    except Exception:
        pass
    ok = mt5.initialize(MT5_TERMINAL_PATH) if os.path.exists(MT5_TERMINAL_PATH) else mt5.initialize()
    if not ok and os.path.exists(MT5_TERMINAL_PATH):
        ok = mt5.initialize()
    if not ok:
        print(f"[MT5 Feed] Initialization failed: {mt5.last_error()}")
        return False
    return True


def shutdown_mt5():
    """Close connection to MT5 terminal."""
    mt5.shutdown()


def get_live_tick(symbol: str = "EURUSD") -> Optional[dict]:
    """Fetch live bid, ask, and spread for a symbol."""
    if not initialize_mt5():
        return None

    if not mt5.symbol_select(symbol, True):
        return None
    tick = mt5.symbol_info_tick(symbol)
    if not tick:
        return None

    is_jpy = "JPY" in symbol
    pip_mult = 100.0 if is_jpy else 10000.0
    spread_pips = (tick.ask - tick.bid) * pip_mult

    return {
        "symbol": symbol,
        "bid": float(tick.bid),
        "ask": float(tick.ask),
        "spread_pips": round(float(spread_pips), 2),
        "time": pd.to_datetime(mt5_ts_to_utc_epoch(tick.time), unit="s", utc=True),
    }


def fetch_candles(
    symbol: str = "EURUSD",
    timeframe: int = mt5.TIMEFRAME_M15,
    count: int = 500,
    completed_only: bool = True
):
    """Fetch the latest N completed candles for a symbol in True UTC."""
    if not initialize_mt5():
        return None

    if not mt5.symbol_select(symbol, True):
        return None

    start_pos = 1 if completed_only else 0
    rates = mt5.copy_rates_from_pos(symbol, timeframe, start_pos, count)
    if rates is None or len(rates) == 0:
        print(f"[MT5 Feed] Failed to fetch candles for {symbol}: {mt5.last_error()}")
        return None

    df = pd.DataFrame(rates)
    df["time"] = mt5_server_ts_to_utc_series(df["time"]).values
    df["time"] = pd.to_datetime(df["time"], utc=True)
    df.rename(columns={"tick_volume": "volume"}, inplace=True)
    return df[["time", "open", "high", "low", "close", "volume"]]


if __name__ == "__main__":
    print("Testing MT5 Feed Adapter...")
    tick = get_live_tick("EURUSD")
    print("Live EUR/USD:", tick)
    bars = fetch_candles("EURUSD", count=5)
    print("\nRecent 5 Candles:")
    print(bars)
    shutdown_mt5()
