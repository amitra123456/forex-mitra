r"""
Pair-Specific GBPJPY (Purged Walk-Forward MTF+Macro+COT + VIX-Unwind)
& EURUSD (Transatlantic Sovereign Bond-Leads-FX H4 Convergence) Engine
======================================================================
Created from the 9-Phase `/goal` Study (`2014-2026` M1/M5/M15/H1/H4/D1/W1 + 29 Global Sovereign
Macro Series + 12-Year Official CFTC COT Archives + Purged 6-Month Walk-Forward Validation):

1. GBPJPY Pair-Specific Engine (`ENABLED: true`, Magic `777490`):
   - Fuses M15 intrabar wick/RV microstructure, H1/H4/D1/W1 momentum & live 20d Variance Ratio,
     Dual-Leg (`GBPUSD` & `USDJPY`) 4h/24h z-scores, UK Gilt Yield (`IGLT.L`), US 5Y/10Y Yields (`^FVX, ^TNX`),
     Corporate Credit Spread (`HYG/IEF`), CBOE `^VIX` 5d shock, WTI Oil (`CL=F`), and Official CFTC `GBP-JPY`
     Commercial Hedger Positioning (`cot_gbpjpy_comm_z_spread`).
   - Asymmetric Carry Thresholds: `+1.30z` Long (earning `+0.3` positive MT5 swap, 24h max hold) vs
     `-1.60z` Short (overcoming `-2.2` negative MT5 swap, 20h max hold), plus `VIX/Credit` Crash Unwind Short.
   - Validated across 9 consecutive Purged Walk-Forward OOS years (`2018-2026`: `+29.1R` at `1x`, `+22.6R` at `2x`,
     `+9.9R` at `3x`, `7/9` positive years, `63.0%` active winning months in `2024-2026`, verified on `M5` & `M1` bars).

2. EURUSD Sovereign Bond-Leads-FX H4 Engine (`ENABLED: false` by default, Magic `777390`):
   - Sole 12-year (`2015-2026`) `1x/2x/3x`-cost survivor out of 32 tested EURUSD hypotheses (`|IEF/EXVM.DE z20| >= 0.85`
     leading lagging `EURUSD |d1_z20| < 0.60` + 12-bar H4 breakout). Kept disabled by default because 2024-2026 OOS
     was flat/slightly negative (`-1.4R` over 25 trades) and retail EURUSD carries negative swap on both sides.

All Permanent Directives Enforced:
  - Breakeven protection is PERMANENTLY DISABLED.
  - Global Sunday Reopen Blackout until Monday `00:00:00 UTC`.
  - Mandatory Friday `15:45 America/New_York` Auto-Liquidation.
  - Governed sizing via `trading/portfolio_risk_governor.py` + `send_guarded_entry_order` symbol mutex.
"""

import io
import json
import math
import sys
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import joblib
import MetaTrader5 as mt5
import numpy as np
import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from trading.mt5_feed import initialize_mt5, shutdown_mt5, fetch_candles, mt5_ts_to_utc_epoch
from trading.bot_manager import get_filling_mode, close_mt5_position, send_guarded_entry_order
from trading.market_hours import is_market_open
from trading.portfolio_risk_governor import get_governed_risk_state
from trading.boxx_cash_sleeve import manage_boxx_cash_sleeve, MAGIC_BOXX
from trading.external_macro_feed import get_live_macro_snapshot, compute_jpy_macro_score

NY_TZ = ZoneInfo("America/New_York")
DATA_DIR = PROJECT_ROOT / "data" / "goal_eurusd_gbpjpy"
MODEL_GJ_PATH = PROJECT_ROOT / "models" / "gbpjpy_purged_wf_lgbm_2026h2.joblib"
CONFIG_PATH = PROJECT_ROOT / "trading" / "pair_specific_gj_eu_config.json"
TELEMETRY_PATH = PROJECT_ROOT / "trading" / "telemetry_PAIR_SPECIFIC_GJ_EU.json"
MACRO_COT_CACHE = PROJECT_ROOT / "data" / "cache" / "live_gj_eu_sovereign_cot.json"
LOG_PATH = PROJECT_ROOT / "logs" / "pair_specific_gj_eu.log"
LOG_PATH.parent.mkdir(parents=True, exist_ok=True)

MAGIC_GJ_WF = 777490
MAGIC_EU_BC = 777390


def log(msg: str):
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    line = f"[{ts}] [GJ_EU_SPEC] {msg}"
    print(line, flush=True)
    try:
        with open(LOG_PATH, "a", encoding="utf-8", errors="replace") as f:
            f.write(line + "\n")
    except Exception:
        pass


def load_or_init_config() -> dict:
    default_cfg = {
        "GBPJPY_PURGED_WF": {
            "ENABLED": True,
            "MAGIC": MAGIC_GJ_WF,
            "RISK_SCALE_OF_GOVERNOR": 1.00,
            "LONG_Z_THRESH": 1.30,
            "SHORT_Z_THRESH": -1.60,
            "SL_ATR_MULT": 2.2,
            "TP_ATR_MULT": 2.8,
            "MAX_HOLD_HOURS_LONG": 24,
            "MAX_HOLD_HOURS_SHORT": 20,
            "ENABLE_VIX_CREDIT_UNWIND": True,
            "ENABLE_RISK_OFF_LONG_VETO": True,
            "RISK_OFF_VIX_Z5_MAX": 1.0,
            "RISK_OFF_CREDIT_Z5_MIN": -1.0,
            "RISK_OFF_JPY_MACRO_MIN": -0.20,
            "BREAKEVEN_PROTECTION": "PERMANENTLY_DISABLED_BY_DESIGN",
        },
        "EURUSD_BOND_CONVERGENCE_H4": {
            "ENABLED": False,
            "NOTE": "Disabled by default: sole 12Y survivor (+16.9R 2015-2026), but flat (-1.4R / 25tr) in 2024-2026 OOS.",
            "MAGIC": MAGIC_EU_BC,
            "RISK_SCALE_OF_GOVERNOR": 0.50,
            "BOND_Z20_MIN": 0.85,
            "EURUSD_D1_Z20_MAX": 0.60,
            "H4_BREAKOUT_BARS": 12,
            "SL_ATR_MULT": 2.2,
            "TP_ATR_MULT": 3.0,
            "MAX_HOLD_H4_BARS": 20,
            "BREAKEVEN_PROTECTION": "PERMANENTLY_DISABLED_BY_DESIGN",
        },
        "BOXX_CASH_SLEEVE": {
            "ENABLED": True,
            "MAGIC": MAGIC_BOXX,
            "TARGET_EQUITY_FRAC": 0.25,
            "MAX_ALPHA_MARGIN_TO_ENTER_FRAC": 0.35,
            "RELEASE_ALPHA_MARGIN_FRAC": 0.42,
            "MAX_SPREAD_USD": 0.03,
            "NOTE": "Deploys 25% idle cash into MT5 BOXX Mon-Wed US RTH; auto-releases if alpha margin >=42%; flattens Fri 15:45 NY.",
        },
    }
    if not CONFIG_PATH.exists():
        with open(CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(default_cfg, f, indent=2)
        return default_cfg
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default_cfg


def get_live_sovereign_and_cot_features(max_age_sec: int = 14400) -> dict:
    """Fetches causal sovereign bond/yield/credit/VIX/oil z-scores + CFTC GBP-JPY Commercial COT spread."""
    if MACRO_COT_CACHE.exists():
        try:
            if time.time() - MACRO_COT_CACHE.stat().st_mtime < max_age_sec:
                with open(MACRO_COT_CACHE, "r", encoding="utf-8") as f:
                    return json.load(f)
        except Exception:
            pass

    snap = {
        "uk_gilt_yield_z20": 0.0,
        "us_yield_5_10_z20": 0.0,
        "credit_hyg_ief_z20": 0.0,
        "credit_hyg_ief_z5": 0.0,
        "vix_z5": 0.0,
        "oil_z20": 0.0,
        "eu_us_bond_price_spread_z20": 0.0,
        "cot_gbpjpy_comm_z_spread": 0.0,
    }

    # 1. Load latest COT from Phase 1 archive + live CFTC current-week text feed if newer
    try:
        cot_pq = DATA_DIR / "cftc_cot_fx_2014_2026.parquet"
        if cot_pq.exists():
            cdf = pd.read_parquet(cot_pq).sort_values("avail_time")
            cdf = cdf[cdf["avail_time"] <= pd.Timestamp(datetime.now(timezone.utc))]
            if len(cdf):
                snap["cot_gbpjpy_comm_z_spread"] = round(float(cdf["cot_gbpjpy_comm_z_spread"].iloc[-1]), 4)
    except Exception:
        pass

    # 2. Sovereign Yields, Credit, VIX, Oil, and EU-US Bond Spread via yfinance (strictly completed NY closes)
    try:
        import yfinance as yf
        tks = ["IGLT.L", "^FVX", "^TNX", "HYG", "IEF", "^VIX", "CL=F", "EXVM.DE"]
        raw = yf.download(tks, period="9mo", interval="1d", progress=False)["Close"].sort_index().ffill().bfill()
        now_ny = datetime.now(NY_TZ)
        if len(raw) > 85:
            if raw.index[-1].date() == now_ny.date() and now_ny.hour < 17:
                raw = raw.iloc[:-1]

            def zd(s, lb=20, vlb=60):
                d = float(s.iloc[-1] - s.iloc[-1 - lb])
                sd = float(s.diff(1).iloc[-vlb:].std()) * math.sqrt(float(lb))
                return float(np.clip(d / (sd + 1e-9), -4.0, 4.0))

            def zr(s, lb=20, vlb=60):
                lr = float(math.log(max(float(s.iloc[-1]), 1e-9) / max(float(s.iloc[-1 - lb]), 1e-9)))
                sd = float(np.log(s / s.shift(1)).iloc[-vlb:].std()) * math.sqrt(float(lb))
                return float(np.clip(lr / (sd + 1e-9), -4.0, 4.0))

            snap["uk_gilt_yield_z20"] = round(-zr(raw["IGLT.L"], 20), 4)
            snap["us_yield_5_10_z20"] = round(0.5 * zd(raw["^FVX"], 20) + 0.5 * zd(raw["^TNX"], 20), 4)
            snap["credit_hyg_ief_z20"] = round(zr(raw["HYG"] / (raw["IEF"] + 1e-9), 20), 4)
            snap["credit_hyg_ief_z5"] = round(zr(raw["HYG"] / (raw["IEF"] + 1e-9), 5), 4)
            snap["vix_z5"] = round(zd(raw["^VIX"], 5), 4)
            snap["oil_z20"] = round(zr(raw["CL=F"], 20), 4)
            snap["eu_us_bond_price_spread_z20"] = round(zr(raw["IEF"] / (raw["EXVM.DE"] + 1e-9), 20), 4)
    except Exception:
        pass

    try:
        with open(MACRO_COT_CACHE, "w", encoding="utf-8") as f:
            json.dump(snap, f, indent=2)
    except Exception:
        pass
    return snap


class PairSpecificGBPJPYEURUSDEngine:
    def __init__(self):
        self.cfg = load_or_init_config()
        self.gj_bundle = joblib.load(MODEL_GJ_PATH) if MODEL_GJ_PATH.exists() else None
        self.last_eval_bar = {}
        log("Initialized Pair-Specific GBPJPY (Purged Walk-Forward MTF+Macro+COT) & EURUSD Engine.")

    def size_fx_lots(self, sym: str, sl_dist: float, risk_scale: float) -> tuple:
        gov = get_governed_risk_state()
        sizing_cap = float(gov.get("sizing_capital", 100000.0))
        eff_risk_pct = float(gov.get("effective_risk_pct", 0.80)) * float(risk_scale)
        info = mt5.symbol_info(sym)
        if info is None or sl_dist <= 0:
            return 0.0, eff_risk_pct
        dpp = float(info.trade_tick_value) / max(float(info.trade_tick_size), 1e-9)
        raw_lots = (sizing_cap * (eff_risk_pct / 100.0)) / max(sl_dist * dpp, 1e-6)
        vmin, vmax, vstep = float(info.volume_min), float(info.volume_max), float(info.volume_step)
        steps = math.floor(max(vmin, min(vmax, raw_lots)) / vstep + 1e-9)
        return round(max(vmin, min(10.0, steps * vstep)), 2), eff_risk_pct

    def manage_positions_and_friday_flat(self, now_utc: datetime) -> bool:
        dt_ny = now_utc.astimezone(NY_TZ)
        all_pos = [p for p in (mt5.positions_get() or []) if p.magic in (MAGIC_GJ_WF, MAGIC_EU_BC)]

        # 1. Mandatory Friday 15:45 NY Auto-Liquidation
        if dt_ny.weekday() == 4 and (dt_ny.hour > 15 or (dt_ny.hour == 15 and dt_ny.minute >= 45)):
            for p in all_pos:
                close_mt5_position(p.ticket, p.symbol)
                log(f"[FRIDAY 15:45 NY FLAT] Closed #{p.ticket} {p.symbol}")
            return True

        # 2. Max-Hold Time Horizon Expiry
        gj_cfg = self.cfg.get("GBPJPY_PURGED_WF", {})
        eu_cfg = self.cfg.get("EURUSD_BOND_CONVERGENCE_H4", {})
        for p in all_pos:
            open_ep = mt5_ts_to_utc_epoch(int(p.time))
            held_hours = (now_utc.timestamp() - open_ep) / 3600.0
            if p.magic == MAGIC_GJ_WF:
                max_h = float(gj_cfg.get("MAX_HOLD_HOURS_LONG", 24) if p.type == mt5.ORDER_TYPE_BUY else gj_cfg.get("MAX_HOLD_HOURS_SHORT", 20))
                if held_hours >= max_h:
                    close_mt5_position(p.ticket, p.symbol)
                    log(f"[GBPJPY WF MAX-HOLD] Closed #{p.ticket} after {held_hours:.1f}h/{max_h:.0f}h")
            elif p.magic == MAGIC_EU_BC:
                max_h = float(eu_cfg.get("MAX_HOLD_H4_BARS", 20)) * 4.0
                if held_hours >= max_h:
                    close_mt5_position(p.ticket, p.symbol)
                    log(f"[EURUSD BOND-CONV MAX-HOLD] Closed #{p.ticket} after {held_hours:.1f}h/{max_h:.0f}h")
        return False

    def evaluate_gbpjpy(self, now_utc: datetime, macro_snap: dict) -> dict:
        gj_cfg = self.cfg.get("GBPJPY_PURGED_WF", {})
        if not gj_cfg.get("ENABLED", True) or self.gj_bundle is None:
            return {"status": "DISABLED", "pred_z": 0.0}

        mkt_ok, rsn, _ = is_market_open("GBPJPY", now_utc)
        if not mkt_ok or now_utc.weekday() > 3 or not (6 <= now_utc.hour <= 16):
            return {"status": "OFF_SESSION", "detail": rsn if not mkt_ok else f"Active Mon-Thu 06-16 UTC (cur {now_utc.hour:02d}:00)", "pred_z": 0.0}

        h1 = fetch_candles("GBPJPY", mt5.TIMEFRAME_H1, count=3000, completed_only=True)
        m15 = fetch_candles("GBPJPY", mt5.TIMEFRAME_M15, count=12, completed_only=True)
        gu = fetch_candles("GBPUSD", mt5.TIMEFRAME_H1, count=180, completed_only=True)
        uj = fetch_candles("USDJPY", mt5.TIMEFRAME_H1, count=180, completed_only=True)
        if h1 is None or len(h1) < 2900 or m15 is None or gu is None or uj is None:
            return {"status": "DATA_WAIT", "pred_z": 0.0}

        last_bar_t = str(h1["time"].iloc[-1])
        c, h, l = h1["close"], h1["high"], h1["low"]
        tr = np.maximum(h - l, np.maximum((h - c.shift(1)).abs(), (l - c.shift(1)).abs()))
        atr20 = float(tr.rolling(20).mean().iloc[-1])
        r1 = np.log(c / c.shift(1)).fillna(0.0)
        rvol = float(r1.rolling(120).std().iloc[-1])

        def rz(ser, k):
            sd = float(np.log(ser / ser.shift(1)).rolling(120).std().iloc[-1]) * math.sqrt(float(k))
            return float(np.clip(math.log(float(ser.iloc[-1]) / float(ser.iloc[-1 - k])) / (sd + 1e-9), -4.0, 4.0))

        r12 = np.log(c / c.shift(12)).fillna(0.0)
        vr12 = float(np.clip(r12.iloc[-480:].var() / (12.0 * r1.iloc[-480:].var() + 1e-12), 0.4, 2.0))

        def rsi_norm(ser, rn):
            d = ser.diff()
            up = d.where(d > 0, 0.0).ewm(alpha=1.0 / rn, adjust=False).mean().iloc[-1]
            dn = (-d.where(d < 0, 0.0)).ewm(alpha=1.0 / rn, adjust=False).mean().iloc[-1]
            return float(((100.0 - 100.0 / (1.0 + up / (dn + 1e-9))) - 50.0) / 25.0)

        bb_z24 = float(np.clip((c.iloc[-1] - c.iloc[-24:].mean()) / (c.iloc[-24:].std() + 1e-9), -4.0, 4.0))
        m_last4 = m15.tail(4)
        sb = m_last4["close"] - m_last4["open"]
        w_imb = ((np.minimum(m_last4["open"], m_last4["close"]) - m_last4["low"]) - (m_last4["high"] - np.maximum(m_last4["open"], m_last4["close"]))).sum()

        feat_map = {
            "ret_4h_z": rz(c, 4), "ret_12h_z": rz(c, 12), "ret_1d_z": rz(c, 24),
            "ret_5d_z": rz(c, 120), "ret_20d_z": rz(c, 480), "ret_60d_z": rz(c, 1440), "ret_12w_z": rz(c, 2880),
            "live_vr_12h": vr12, "rsi2_h1": rsi_norm(c, 2), "rsi14_h1": rsi_norm(c, 14), "bb_z24": bb_z24,
            "m15_wick_norm": float(np.clip(w_imb / (atr20 + 1e-9), -3.0, 3.0)),
            "m15_rv_ratio": float(np.clip(math.sqrt(float((sb ** 2).sum())) / (atr20 + 1e-9), 0.0, 4.0)),
            "m15_up_centered": float((sb > 0).mean()) - 0.5,
            "gu_z24": rz(gu["close"], 24), "uj_z24": rz(uj["close"], 24),
            "gu_z4": rz(gu["close"], 4), "uj_z4": rz(uj["close"], 4),
            "uk_gilt_yield_z20": macro_snap["uk_gilt_yield_z20"],
            "us_yield_5_10_z20": macro_snap["us_yield_5_10_z20"],
            "credit_hyg_ief_z20": macro_snap["credit_hyg_ief_z20"],
            "vix_z5": macro_snap["vix_z5"],
            "oil_z20": macro_snap["oil_z20"],
            "cot_gbpjpy_comm_z_spread": macro_snap["cot_gbpjpy_comm_z_spread"],
        }

        fcols = self.gj_bundle["feature_cols"]
        x_vec = np.array([[float(feat_map.get(col, 0.0)) for col in fcols]], dtype=np.float64)
        x_std = (x_vec - self.gj_bundle["scaler_mean"]) / self.gj_bundle["scaler_std"]
        raw_pred = float(self.gj_bundle["model"].predict(x_std)[0])
        pred_z = (raw_pred - self.gj_bundle["pred_train_mean"]) / self.gj_bundle["pred_train_std"]

        # Check existing GBPJPY position across ALL engines (mutex)
        if mt5.positions_get(symbol="GBPJPY"):
            return {
                "status": "IN_POSITION_OR_MUTEX",
                "last_close": round(float(c.iloc[-1]), 3),
                "pred_z": round(pred_z, 3),
                "long_thr": gj_cfg.get("LONG_Z_THRESH", 1.30),
                "short_thr": gj_cfg.get("SHORT_Z_THRESH", -1.60),
                "vr_12h": round(vr12, 3),
                "cot_z": round(float(macro_snap["cot_gbpjpy_comm_z_spread"]), 2),
                "uk_gilt_z": round(float(macro_snap["uk_gilt_yield_z20"]), 2),
                "vix_z5": round(float(macro_snap["vix_z5"]), 2),
                "bar": last_bar_t[:16],
            }

        sig = 0
        sl_m, tp_m, tag = float(gj_cfg.get("SL_ATR_MULT", 2.2)), float(gj_cfg.get("TP_ATR_MULT", 2.8)), "GJ_WF"
        lo24 = float(l.iloc[-25:-1].min())
        if gj_cfg.get("ENABLE_VIX_CREDIT_UNWIND", True) and (macro_snap["vix_z5"] > 1.0 or macro_snap["credit_hyg_ief_z5"] < -1.0):
            if float(c.iloc[-1]) < lo24 and feat_map["gu_z4"] < 0 and feat_map["uj_z4"] < 0:
                sig = -1; sl_m = 2.0; tp_m = 3.5; tag = "GJ_VIX_UNWIND"

        if sig == 0:
            if pred_z >= float(gj_cfg.get("LONG_Z_THRESH", 1.30)):
                sig = 1
            elif pred_z <= float(gj_cfg.get("SHORT_Z_THRESH", -1.60)):
                sig = -1

        if sig == 1 and gj_cfg.get("ENABLE_RISK_OFF_LONG_VETO", True):
            vix_z5 = float(macro_snap.get("vix_z5", 0.0))
            cr_z5 = float(macro_snap.get("credit_hyg_ief_z5", 0.0))
            vix_max = float(gj_cfg.get("RISK_OFF_VIX_Z5_MAX", 1.0))
            cr_min = float(gj_cfg.get("RISK_OFF_CREDIT_Z5_MIN", -1.0))
            jm_min = float(gj_cfg.get("RISK_OFF_JPY_MACRO_MIN", -0.20))
            try:
                ext_snap = get_live_macro_snapshot()
                jpy_m_sc = float(compute_jpy_macro_score("GBPJPY", 1, ext_snap))
            except Exception:
                jpy_m_sc = 0.0
            reasons = []
            if vix_z5 > vix_max:
                reasons.append(f"VIX_Z5={vix_z5:+.2f}>{vix_max:+.2f}")
            if cr_z5 < cr_min:
                reasons.append(f"Credit_Z5={cr_z5:+.2f}<{cr_min:+.2f}")
            if jpy_m_sc < jm_min:
                reasons.append(f"JPY_Macro={jpy_m_sc:+.2f}<{jm_min:+.2f}")
            if reasons:
                detail = "; ".join(reasons)
                if self.last_eval_bar.get("GBPJPY_RISKOFF_VETO") != last_bar_t:
                    log(f"[RISK-OFF LONG VETO] Blocked GBPJPY BUY (PredZ={pred_z:+.2f}): {detail}")
                    self.last_eval_bar["GBPJPY_RISKOFF_VETO"] = last_bar_t
                return {
                    "status": "RISK_OFF_LONG_VETO",
                    "detail": detail,
                    "last_close": round(float(c.iloc[-1]), 3),
                    "pred_z": round(pred_z, 3),
                    "long_thr": gj_cfg.get("LONG_Z_THRESH", 1.30),
                    "short_thr": gj_cfg.get("SHORT_Z_THRESH", -1.60),
                    "vr_12h": round(vr12, 3),
                    "cot_z": round(float(macro_snap["cot_gbpjpy_comm_z_spread"]), 2),
                    "uk_gilt_z": round(float(macro_snap["uk_gilt_yield_z20"]), 2),
                    "vix_z5": round(vix_z5, 2),
                    "credit_z5": round(cr_z5, 2),
                    "jpy_macro_score": round(jpy_m_sc, 2),
                    "bar": last_bar_t[:16],
                }

        if sig != 0:
            jpy_pos = [p for p in (mt5.positions_get() or []) if str(p.symbol).endswith("JPY") and p.symbol != "GBPJPY"]
            if any((1 if p.type == mt5.ORDER_TYPE_BUY else -1) != sig for p in jpy_pos):
                return {
                    "status": "JPY_MUTEX",
                    "last_close": round(float(c.iloc[-1]), 3),
                    "pred_z": round(pred_z, 3),
                    "long_thr": gj_cfg.get("LONG_Z_THRESH", 1.30),
                    "short_thr": gj_cfg.get("SHORT_Z_THRESH", -1.60),
                    "vr_12h": round(vr12, 3),
                    "cot_z": round(float(macro_snap["cot_gbpjpy_comm_z_spread"]), 2),
                    "uk_gilt_z": round(float(macro_snap["uk_gilt_yield_z20"]), 2),
                    "vix_z5": round(float(macro_snap["vix_z5"]), 2),
                    "bar": last_bar_t[:16],
                }
            if len(jpy_pos) >= 2:
                return {
                    "status": "JPY_GROUP_CAP",
                    "last_close": round(float(c.iloc[-1]), 3),
                    "pred_z": round(pred_z, 3),
                    "long_thr": gj_cfg.get("LONG_Z_THRESH", 1.30),
                    "short_thr": gj_cfg.get("SHORT_Z_THRESH", -1.60),
                    "vr_12h": round(vr12, 3),
                    "cot_z": round(float(macro_snap["cot_gbpjpy_comm_z_spread"]), 2),
                    "uk_gilt_z": round(float(macro_snap["uk_gilt_yield_z20"]), 2),
                    "vix_z5": round(float(macro_snap["vix_z5"]), 2),
                    "bar": last_bar_t[:16],
                }

        if sig != 0 and self.last_eval_bar.get("GBPJPY") != last_bar_t:
            self.last_eval_bar["GBPJPY"] = last_bar_t
            tick = mt5.symbol_info_tick("GBPJPY")
            if tick:
                is_buy = (sig == 1)
                ep = float(tick.ask if is_buy else tick.bid)
                sld = sl_m * atr20
                tpd = tp_m * sld / sl_m
                lot, eff_r = self.size_fx_lots("GBPJPY", sld, float(gj_cfg.get("RISK_SCALE_OF_GOVERNOR", 1.00)))
                if lot > 0:
                    req = {
                        "action": mt5.TRADE_ACTION_DEAL,
                        "symbol": "GBPJPY",
                        "volume": float(lot),
                        "type": mt5.ORDER_TYPE_BUY if is_buy else mt5.ORDER_TYPE_SELL,
                        "price": ep,
                        "sl": round(ep - sld if is_buy else ep + sld, 3),
                        "tp": round(ep + tpd if is_buy else ep - tpd, 3),
                        "deviation": 15,
                        "magic": MAGIC_GJ_WF,
                        "comment": f"ANT {tag} ({eff_r:.2f}%)",
                        "type_time": mt5.ORDER_TIME_GTC,
                        "type_filling": get_filling_mode("GBPJPY"),
                    }
                    res = send_guarded_entry_order(req, log)
                    if res and res.retcode == mt5.TRADE_RETCODE_DONE:
                        log(f"[{tag} FILLED] GBPJPY {'BUY' if is_buy else 'SELL'} {lot}L @ {ep:.3f} | PredZ={pred_z:+.2f} | Ticket=#{res.order}")

        return {
            "status": "SCANNING",
            "last_close": round(float(c.iloc[-1]), 3),
            "pred_z": round(pred_z, 3),
            "long_thr": gj_cfg.get("LONG_Z_THRESH", 1.30),
            "short_thr": gj_cfg.get("SHORT_Z_THRESH", -1.60),
            "vr_12h": round(vr12, 3),
            "cot_z": round(float(macro_snap["cot_gbpjpy_comm_z_spread"]), 2),
            "uk_gilt_z": round(float(macro_snap["uk_gilt_yield_z20"]), 2),
            "vix_z5": round(float(macro_snap["vix_z5"]), 2),
            "bar": last_bar_t[:16],
        }

    def evaluate_eurusd_bond_convergence(self, now_utc: datetime, macro_snap: dict) -> dict:
        eu_cfg = self.cfg.get("EURUSD_BOND_CONVERGENCE_H4", {})
        bz20 = float(macro_snap.get("eu_us_bond_price_spread_z20", 0.0))
        if not eu_cfg.get("ENABLED", False):
            return {"status": "DISABLED_BY_DESIGN", "bond_spread_z20": round(bz20, 3), "detail": "Kept off: -1.4R flat in 2024-26 OOS; negative dual swap"}

        mkt_ok, _, _ = is_market_open("EURUSD", now_utc)
        if not mkt_ok or now_utc.weekday() > 3:
            return {"status": "OFF_SESSION", "bond_spread_z20": round(bz20, 3)}

        h4 = fetch_candles("EURUSD", mt5.TIMEFRAME_H4, count=200, completed_only=True)
        h1 = fetch_candles("EURUSD", mt5.TIMEFRAME_H1, count=600, completed_only=True)
        if h4 is None or len(h4) < 50 or h1 is None or len(h1) < 520:
            return {"status": "DATA_WAIT", "bond_spread_z20": round(bz20, 3)}

        last_bar_t = str(h4["time"].iloc[-1])
        c1 = h1["close"]
        rv1 = float(np.log(c1 / c1.shift(1)).rolling(240).std().iloc[-1])
        d1_z20 = float(math.log(float(c1.iloc[-1]) / float(c1.iloc[-481])) / (rv1 * math.sqrt(480.0) + 1e-9))

        c4, h4h, l4 = h4["close"], h4["high"], h4["low"]
        tr4 = np.maximum(h4h - l4, np.maximum((h4h - c4.shift(1)).abs(), (l4 - c4.shift(1)).abs()))
        atr4 = float(tr4.rolling(14).mean().iloc[-1])
        hi12 = float(h4h.iloc[-13:-1].max())
        lo12 = float(l4.iloc[-13:-1].min())
        b_min = float(eu_cfg.get("BOND_Z20_MIN", 0.85))
        d_max = float(eu_cfg.get("EURUSD_D1_Z20_MAX", 0.60))

        sig = 0
        if bz20 >= b_min and d1_z20 < d_max and float(c4.iloc[-1]) > hi12:
            sig = 1
        elif bz20 <= -b_min and d1_z20 > -d_max and float(c4.iloc[-1]) < lo12:
            sig = -1

        if sig != 0 and not mt5.positions_get(symbol="EURUSD") and self.last_eval_bar.get("EURUSD") != last_bar_t:
            self.last_eval_bar["EURUSD"] = last_bar_t
            tick = mt5.symbol_info_tick("EURUSD")
            if tick:
                is_buy = (sig == 1)
                ep = float(tick.ask if is_buy else tick.bid)
                sld = float(eu_cfg.get("SL_ATR_MULT", 2.2)) * atr4
                tpd = float(eu_cfg.get("TP_ATR_MULT", 3.0)) * atr4
                lot, eff_r = self.size_fx_lots("EURUSD", sld, float(eu_cfg.get("RISK_SCALE_OF_GOVERNOR", 0.50)))
                if lot > 0:
                    req = {
                        "action": mt5.TRADE_ACTION_DEAL,
                        "symbol": "EURUSD",
                        "volume": float(lot),
                        "type": mt5.ORDER_TYPE_BUY if is_buy else mt5.ORDER_TYPE_SELL,
                        "price": ep,
                        "sl": round(ep - sld if is_buy else ep + sld, 5),
                        "tp": round(ep + tpd if is_buy else ep - tpd, 5),
                        "deviation": 10,
                        "magic": MAGIC_EU_BC,
                        "comment": f"ANT EU_BOND ({eff_r:.2f}%)",
                        "type_time": mt5.ORDER_TIME_GTC,
                        "type_filling": get_filling_mode("EURUSD"),
                    }
                    send_guarded_entry_order(req, log)

        return {"status": "SCANNING", "bond_spread_z20": round(bz20, 3), "d1_z20": round(d1_z20, 3), "bar": last_bar_t[:16]}

    def run_cycle(self):
        self.cfg = load_or_init_config()
        now_utc = datetime.now(timezone.utc)
        if now_utc.weekday() == 6:
            return
        if self.manage_positions_and_friday_flat(now_utc):
            return
        ms = get_live_sovereign_and_cot_features()
        gj_st = self.evaluate_gbpjpy(now_utc, ms)
        eu_st = self.evaluate_eurusd_bond_convergence(now_utc, ms)
        bx_st = manage_boxx_cash_sleeve(now_utc, self.cfg.get("BOXX_CASH_SLEEVE", {}), log)
        payload = {
            "timestamp": now_utc.strftime("%Y-%m-%d %H:%M:%S UTC"),
            "sovereign_cot_macro": ms,
            "gbpjpy_purged_wf": gj_st,
            "eurusd_bond_convergence": eu_st,
            "boxx_cash_sleeve": bx_st,
            "breakeven_protection": "PERMANENTLY_DISABLED_BY_DESIGN",
        }
        try:
            with open(TELEMETRY_PATH, "w", encoding="utf-8") as f:
                json.dump(payload, f, indent=2)
        except Exception:
            pass

    def run(self):
        while True:
            try:
                if initialize_mt5():
                    self.run_cycle()
            except Exception as e:
                log(f"Cycle error: {e}")
            time.sleep(30)


if __name__ == "__main__":
    if initialize_mt5():
        PairSpecificGBPJPYEURUSDEngine().run()
