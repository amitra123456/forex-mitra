r"""
External Macro & Scheduled FOMC Bond-Confirmed Drift Feed (ANT_FOREX)
=====================================================================
Pulls external institutional macro drivers outside MT5 (via `yfinance`, cached every 4h,
with automatic MT5 `IEF/TIP/HYG/UUP` fallback if external network is ever unavailable):
  1. US Nominal Treasury Yields: `^FVX` (5Y Fed Path) & `^TNX` (10Y Benchmark)
  2. US Real-Yield & Breakeven Proxies: `TIP` vs `IEF`
  3. Institutional Credit-Spread Appetite: `HYG / IEF` + `^VIX` (Carry-Unwind Shield)
  4. Broad US Dollar Index: `DX-Y.NYB` (`DXY`)
  5. Global Cyclical Commodity Demand: `HG=F / GLD` (Copper/Gold Ratio)

Also provides live intraday `IEF` (10Y US Treasury) & `UUP` (DXY) H1 shock verification
for Scheduled FOMC Rate Decision Drift (`14:00 America/New_York` -> `15:00 NY` H1 close).
"""

import json
import time
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import MetaTrader5 as mt5
import numpy as np
import pandas as pd

from trading.mt5_feed import fetch_candles

PROJECT_ROOT = Path(__file__).resolve().parent.parent
CACHE_DIR = PROJECT_ROOT / "data" / "cache"
CACHE_DIR.mkdir(parents=True, exist_ok=True)
LIVE_MACRO_JSON = CACHE_DIR / "live_external_macro_snapshot.json"
NY_TZ = ZoneInfo("America/New_York")

# Official Federal Reserve FOMC Rate Decision Dates (2025 - 2027)
FOMC_DECISION_DATES = {
    # 2025
    "2025-01-29", "2025-03-19", "2025-05-07", "2025-06-18",
    "2025-07-30", "2025-09-17", "2025-10-29", "2025-12-10",
    # 2026 (including Dec 2026 meeting)
    "2026-01-28", "2026-03-18", "2026-04-29", "2026-06-17",
    "2026-07-29", "2026-09-16", "2026-11-04", "2026-12-09", "2026-12-16",
    # 2027 FOMC Schedule
    "2027-01-27", "2027-03-17", "2027-04-28", "2027-06-09",
    "2027-07-28", "2027-09-15", "2027-10-27", "2027-12-08",
}


def _zscore_diff(s: pd.Series, lookback: int = 20, vol_lb: int = 60) -> float:
    s = s.dropna()
    if len(s) < vol_lb + lookback + 2:
        return 0.0
    d = float(s.iloc[-1] - s.iloc[-1 - lookback])
    sd = float(s.diff(1).iloc[-vol_lb:].std()) * np.sqrt(float(lookback))
    return float(np.clip(d / (sd + 1e-9), -4.0, 4.0))


def _zscore_logret(s: pd.Series, lookback: int = 20, vol_lb: int = 60) -> float:
    s = s.dropna()
    if len(s) < vol_lb + lookback + 2 or float(s.iloc[-1 - lookback]) <= 0:
        return 0.0
    lr = float(np.log(float(s.iloc[-1]) / float(s.iloc[-1 - lookback])))
    sd = float(np.log(s / s.shift(1)).iloc[-vol_lb:].std()) * np.sqrt(float(lookback))
    return float(np.clip(lr / (sd + 1e-9), -4.0, 4.0))


def get_live_macro_snapshot(max_age_sec: int = 14400) -> dict:
    """
    Returns causally completed daily external macro regime metrics (cached for 4 hours).
    Uses completed prior NY daily closes so intraday partial bars never introduce lookahead noise.
    """
    if LIVE_MACRO_JSON.exists():
        try:
            age = time.time() - LIVE_MACRO_JSON.stat().st_mtime
            if age < max_age_sec:
                with open(LIVE_MACRO_JSON, "r", encoding="utf-8") as f:
                    return json.load(f)
        except Exception:
            pass

    snap = {
        "updated_utc": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC"),
        "source": "yfinance_external",
        "us_yield_z20": 0.0,
        "real_yield_z20": 0.0,
        "breakeven_z20": 0.0,
        "credit_risk_on_z20": 0.0,
        "vix_level": 18.0,
        "vix_shock_z5": 0.0,
        "dxy_z20": 0.0,
        "cyclical_cu_au_z20": 0.0,
        "us10y_pct": 0.0,
        "us5y_pct": 0.0,
        "dxy_spot": 0.0,
    }

    try:
        import yfinance as yf
        tickers = ["^FVX", "^TNX", "^VIX", "DX-Y.NYB", "TIP", "IEF", "HYG", "HG=F", "GLD"]
        raw = yf.download(tickers, period="9mo", interval="1d", progress=False)["Close"]
        if raw is not None and len(raw) > 90:
            df = raw.sort_index().ffill().bfill()
            # Drop incomplete today's NY bar if NY market is still open (< 17:00 NY)
            now_ny = datetime.now(NY_TZ)
            if df.index[-1].date() == now_ny.date() and now_ny.hour < 17:
                df = df.iloc[:-1]

            y20 = 0.5 * _zscore_diff(df["^FVX"], 20) + 0.5 * _zscore_diff(df["^TNX"], 20)
            be_ser = df["TIP"] / (df["IEF"] + 1e-9)
            be20 = _zscore_logret(be_ser, 20)
            ry20 = float(np.clip(y20 - 0.7 * be20, -4.0, 4.0))

            cr_ser = df["HYG"] / (df["IEF"] + 1e-9)
            vx5 = _zscore_diff(df["^VIX"], 5)
            cr20 = float(np.clip(0.7 * _zscore_logret(cr_ser, 20) - 0.3 * vx5, -4.0, 4.0))
            dxy20 = _zscore_logret(df["DX-Y.NYB"], 20)
            cu20 = _zscore_logret(df["HG=F"] / (df["GLD"] + 1e-9), 20)

            snap.update({
                "asof_ny_close": str(df.index[-1])[:10],
                "us_yield_z20": round(y20, 3),
                "real_yield_z20": round(ry20, 3),
                "breakeven_z20": round(be20, 3),
                "credit_risk_on_z20": round(cr20, 3),
                "vix_level": round(float(df["^VIX"].iloc[-1]), 2),
                "vix_shock_z5": round(vx5, 3),
                "dxy_z20": round(dxy20, 3),
                "cyclical_cu_au_z20": round(cu20, 3),
                "us10y_pct": round(float(df["^TNX"].iloc[-1]), 3),
                "us5y_pct": round(float(df["^FVX"].iloc[-1]), 3),
                "dxy_spot": round(float(df["DX-Y.NYB"].iloc[-1]), 2),
            })
            with open(LIVE_MACRO_JSON, "w", encoding="utf-8") as f:
                json.dump(snap, f, indent=2)
            return snap
    except Exception:
        pass

    # Automatic MT5 ETF fallback (`IEF`, `TIP`, `HYG`, `UUP`) if external network is down
    try:
        snap["source"] = "mt5_etf_fallback"
        ief = fetch_candles("IEF", mt5.TIMEFRAME_D1, count=120, completed_only=True)
        tip = fetch_candles("TIP", mt5.TIMEFRAME_D1, count=120, completed_only=True)
        hyg = fetch_candles("HYG", mt5.TIMEFRAME_D1, count=120, completed_only=True)
        uup = fetch_candles("UUP", mt5.TIMEFRAME_D1, count=120, completed_only=True)
        if ief is not None and len(ief) > 85:
            # Inverse IEF log-return proxies 10Y yield change!
            y20 = -_zscore_logret(ief["close"], 20)
            snap["us_yield_z20"] = round(y20, 3)
            if tip is not None and len(tip) == len(ief):
                be20 = _zscore_logret(tip["close"] / (ief["close"] + 1e-9), 20)
                snap["breakeven_z20"] = round(be20, 3)
                snap["real_yield_z20"] = round(float(np.clip(y20 - 0.7 * be20, -4.0, 4.0)), 3)
            if hyg is not None and len(hyg) == len(ief):
                snap["credit_risk_on_z20"] = round(_zscore_logret(hyg["close"] / (ief["close"] + 1e-9), 20), 3)
            if uup is not None and len(uup) > 85:
                snap["dxy_z20"] = round(_zscore_logret(uup["close"], 20), 3)
        with open(LIVE_MACRO_JSON, "w", encoding="utf-8") as f:
            json.dump(snap, f, indent=2)
    except Exception:
        pass

    return snap


def compute_jpy_macro_score(sym: str, direction: int, snap: dict) -> float:
    """
    Computes the audited Rate-Differential & Credit-Spread Alignment Score for JPY pairs:
      - USDJPY: driven by US 5Y/10Y yields (+0.60*y20) + DXY (+0.25*dxy20) + Credit (+0.25*cr20),
                minus VIX/credit carry-unwind shock (-0.50 if VIX > 25 & vix_shock_z5 > 1.0).
      - AUDJPY / GBPJPY / EURJPY: driven by Credit Risk-On (+0.55*cr20) + Yields (+0.30*y20)
                + Global Cyclical Copper/Gold (+0.25*cu20) minus carry-unwind panic (-0.80).
    Note: Gold (`XAUUSD, XAUEUR`) intentionally does NOT apply a textbook real-yield veto because
    sovereign central-bank reserve accumulation decoupled Gold from rising US real yields in 2022-2026.
    """
    y20 = float(snap.get("us_yield_z20", 0.0))
    cr20 = float(snap.get("credit_risk_on_z20", 0.0))
    dxy20 = float(snap.get("dxy_z20", 0.0))
    cu20 = float(snap.get("cyclical_cu_au_z20", 0.0))
    vix = float(snap.get("vix_level", 18.0))
    vx5 = float(snap.get("vix_shock_z5", 0.0))

    if sym == "USDJPY":
        panic = -0.50 if (vix > 25.0 and vx5 > 1.0) else 0.0
        bull = 0.60 * y20 + 0.25 * dxy20 + 0.25 * cr20 + panic
        return float(direction * bull)
    if sym in ("AUDJPY", "GBPJPY", "EURJPY"):
        panic = -0.80 if (vix > 24.0 and vx5 > 1.0) else 0.0
        bull = 0.55 * cr20 + 0.30 * y20 + 0.25 * cu20 + panic
        return float(direction * bull)
    return 0.0


def evaluate_fomc_bond_confirmed_drift(now_utc: datetime) -> list:
    """
    Evaluates the audited Post-FOMC (14:00 NY H1 bar close -> 15:00-15:25 NY execution window)
    Bond-Confirmed Drift on `XAUUSD` and `USDJPY`:
      - Requires today's NY date to be an official FOMC Rate Decision date and NY hour == 15 (just after 14:00-15:00 NY bar closed).
      - Requires live completed H1 `IEF` (7-10Y US Treasury ETF) 1-hour shock >= 0.18%
        (`> +0.18%` = dovish yield drop -> BUY XAUUSD / SELL USDJPY; `< -0.18%` = hawkish yield jump -> SELL XAUUSD / BUY USDJPY).
      - Requires `UUP` (US Dollar ETF) not to contradict, and target asset's 14:00 NY H1 candle body >= 0.60 * ATR(20) in same direction.
    Returns list of dicts: `[{"sym": sym, "sig": +/-1, "atr": atr_v, "sl_dist": 2.0*atr_v, "bar_t": bar_t, "ief_ret": ief_r1}]`.
    """
    dt_ny = now_utc.astimezone(NY_TZ)
    ny_date_str = dt_ny.strftime("%Y-%m-%d")
    if ny_date_str not in FOMC_DECISION_DATES or dt_ny.hour != 15 or dt_ny.minute > 35:
        return []

    mt5.symbol_select("IEF", True)
    mt5.symbol_select("UUP", True)
    ief_bars = fetch_candles("IEF", mt5.TIMEFRAME_H1, count=5, completed_only=True)
    uup_bars = fetch_candles("UUP", mt5.TIMEFRAME_H1, count=5, completed_only=True)
    if ief_bars is None or len(ief_bars) < 3:
        return []

    # Verify latest completed IEF bar is the 14:00 NY FOMC bar
    last_ief_ny = pd.Timestamp(ief_bars["time"].iloc[-1]).tz_convert(NY_TZ)
    if last_ief_ny.strftime("%Y-%m-%d") != ny_date_str or last_ief_ny.hour != 14:
        return []

    ief_r1 = float(ief_bars["close"].iloc[-1] / ief_bars["close"].iloc[-2] - 1.0)
    if abs(ief_r1) < 0.0018:
        return []

    uup_r1 = 0.0
    if uup_bars is not None and len(uup_bars) >= 2:
        uup_r1 = float(uup_bars["close"].iloc[-1] / uup_bars["close"].iloc[-2] - 1.0)

    dovish = ief_r1 > 0.0
    if uup_r1 != 0.0 and ((dovish and uup_r1 > 0.0015) or ((not dovish) and uup_r1 < -0.0015)):
        return []

    signals = []
    for sym, ev_dir in [("XAUUSD", 1 if dovish else -1), ("USDJPY", -1 if dovish else 1)]:
        bars = fetch_candles(sym, mt5.TIMEFRAME_H1, count=30, completed_only=True)
        if bars is None or len(bars) < 22:
            continue
        c, h, l, o = bars["close"], bars["high"], bars["low"], bars["open"]
        tr = np.maximum(h - l, np.maximum((h - c.shift(1)).abs(), (l - c.shift(1)).abs()))
        atr_v = float(tr.rolling(20).mean().iloc[-1])
        if np.isnan(atr_v) or atr_v <= 0:
            continue
        bar_move = float(c.iloc[-1] - o.iloc[-1]) * ev_dir
        if bar_move >= 0.60 * atr_v:
            signals.append({
                "sym": sym,
                "sig": ev_dir,
                "atr": atr_v,
                "sl_dist": 2.0 * atr_v,
                "bar_t": f"FOMC_{str(bars['time'].iloc[-1])[:16]}",
                "ief_ret": round(ief_r1 * 100.0, 3),
            })
    return signals
