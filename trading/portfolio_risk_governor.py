r"""
Causal Portfolio Volatility-Target & Drawdown Risk Governor (ANT_FOREX)
=======================================================================
Implements the audited dispersion-compression risk governor (`Row 2` / `Row 5` in
`research/audit_variance_compression_methods.py`) across both live engines:
  1. Base Risk Per Trade: `0.80%` of sizing capital (replaces fixed `1.00%`).
  2. Causal 20-Day Realized Volatility Cap (`Target Annual Vol = 10.0%`):
     Measures trailing 20 completed NY trading days of account equity returns (`sigma_20d`)
     and caps base risk by `min(0.80%, clip(10.0% / sigma_20d, 0.35%, 1.05%))`.
  3. Two-Tier Drawdown Governor (Convex Left-Tail Protection):
     - Drawdown >= `7.0%` below High-Water Mark  -> `0.65x` risk multiplier
     - Drawdown >= `12.0%` below High-Water Mark -> `0.45x` risk multiplier
  4. Optional Split-Account / Portable-Alpha Support (`REFERENCE_CAPITAL_USD`):
     - When `null` / `0` (default): sizes directly off live MT5 `account_info().equity`.
     - When set (e.g., `100000.0` total wealth with `$75k` in external `SGOV`/`BOXX` and
       `MT5_ACTIVE_DEPOSIT_USD = 25000.0` in MT5): sizes risk off `REFERENCE_CAPITAL_USD + (mt5_equity - mt5_deposit)`,
       while actual MT5 margin guards ALWAYS bind against real MT5 cash equity.
"""

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import MetaTrader5 as mt5
import numpy as np
import pandas as pd

from trading.mt5_feed import mt5_ts_to_utc_epoch

PROJECT_ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = PROJECT_ROOT / "trading" / "cross_asset_config.json"
LEDGER_PATH = PROJECT_ROOT / "data" / "cache" / "portfolio_equity_ledger.json"
LEDGER_PATH.parent.mkdir(parents=True, exist_ok=True)
NY_TZ = ZoneInfo("America/New_York")


def _load_governor_cfg() -> dict:
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            cfg = json.load(f)
        return cfg.get("RISK_GOVERNOR", {})
    except Exception:
        return {}


def _load_or_seed_equity_ledger(cur_mt5_equity: float) -> dict:
    """
    Loads persistent daily NY-close equity ledger, seeding initial history from MT5
    closed deals (`history_deals_get`) if no ledger file exists yet.
    """
    ledger = {"hwm_mt5_equity": float(cur_mt5_equity), "daily_closes": {}}
    if LEDGER_PATH.exists():
        try:
            with open(LEDGER_PATH, "r", encoding="utf-8") as f:
                loaded = json.load(f)
            if isinstance(loaded, dict) and loaded.get("daily_closes"):
                ledger = loaded
        except Exception:
            pass

    if not ledger["daily_closes"]:
        # Reconstruct historical daily close balances from MT5 deal history
        try:
            start_dt = datetime(2025, 1, 1)
            end_dt = datetime.now() + timedelta(days=2)
            deals = mt5.history_deals_get(start_dt, end_dt) or []
            if deals:
                rows = []
                running_bal = 0.0
                peak_bal = 0.0
                for d in deals:
                    utc_ep = mt5_ts_to_utc_epoch(int(d.time))
                    dt_ny = datetime.fromtimestamp(utc_ep, tz=timezone.utc).astimezone(NY_TZ)
                    ny_day = (dt_ny - timedelta(hours=17)).strftime("%Y-%m-%d")
                    net_chg = float(d.profit) + float(getattr(d, "swap", 0.0)) + float(getattr(d, "commission", 0.0)) + float(getattr(d, "fee", 0.0))
                    running_bal += net_chg
                    if running_bal > peak_bal:
                        peak_bal = running_bal
                    rows.append((ny_day, round(running_bal, 2)))
                for day_s, bal_v in rows:
                    if bal_v > 0:
                        ledger["daily_closes"][day_s] = bal_v
                ledger["hwm_mt5_equity"] = max(peak_bal, float(cur_mt5_equity))
        except Exception:
            pass

    # Update today's running mark and High-Water Mark
    now_ny = datetime.now(timezone.utc).astimezone(NY_TZ)
    cur_ny_day = (now_ny - timedelta(hours=17)).strftime("%Y-%m-%d")
    ledger["daily_closes"][cur_ny_day] = round(float(cur_mt5_equity), 2)
    if float(cur_mt5_equity) > float(ledger.get("hwm_mt5_equity", 0.0)):
        ledger["hwm_mt5_equity"] = round(float(cur_mt5_equity), 2)

    # Keep last 90 daily closes
    sorted_days = sorted(ledger["daily_closes"].keys())[-90:]
    ledger["daily_closes"] = {k: ledger["daily_closes"][k] for k in sorted_days}

    try:
        tmp = LEDGER_PATH.with_suffix(".json.tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(ledger, f, indent=2)
        tmp.replace(LEDGER_PATH)
    except Exception:
        pass

    return ledger


def get_governed_risk_state() -> dict:
    """
    Computes the causal governed risk percentage and sizing capital for both live engines.
    Strictly excludes today's incomplete NY session when computing trailing 20-day volatility.
    """
    gcfg = _load_governor_cfg()
    acc = mt5.account_info()
    mt5_equity = float(acc.equity) if acc else 100000.0
    ledger = _load_or_seed_equity_ledger(mt5_equity)

    enabled = bool(gcfg.get("ENABLED", True))
    base_risk_pct = float(gcfg.get("BASE_RISK_PCT", 0.80))
    target_vol_pct = float(gcfg.get("TARGET_ANNUAL_VOL_PCT", 10.0))
    min_vol_risk = float(gcfg.get("MIN_VOL_RISK_PCT", 0.35))
    max_vol_risk = float(gcfg.get("MAX_VOL_RISK_PCT", 1.05))
    dd_t1_pct = float(gcfg.get("DD_TIER1_PCT", 7.0))
    dd_t1_mult = float(gcfg.get("DD_TIER1_MULT", 0.65))
    dd_t2_pct = float(gcfg.get("DD_TIER2_PCT", 12.0))
    dd_t2_mult = float(gcfg.get("DD_TIER2_MULT", 0.45))

    ref_cap = gcfg.get("REFERENCE_CAPITAL_USD")
    mt5_dep = gcfg.get("MT5_ACTIVE_DEPOSIT_USD")
    hwm_mt5 = max(float(ledger.get("hwm_mt5_equity", mt5_equity)), mt5_equity)

    if ref_cap and float(ref_cap) > 0:
        base_dep = float(mt5_dep) if (mt5_dep and float(mt5_dep) > 0) else mt5_equity
        sizing_capital = max(1000.0, float(ref_cap) + (mt5_equity - base_dep))
        hwm_capital = max(sizing_capital, float(ref_cap) + (hwm_mt5 - base_dep))
        cap_mode = f"SPLIT_REF(${float(ref_cap):,.0f})"
    else:
        sizing_capital = mt5_equity
        hwm_capital = hwm_mt5
        cap_mode = "MT5_EQUITY"

    # Compute trailing 20 completed daily returns (excluding today's in-progress NY date)
    now_ny = datetime.now(timezone.utc).astimezone(NY_TZ)
    cur_ny_day = (now_ny - timedelta(hours=17)).strftime("%Y-%m-%d")
    completed_items = [(d, v) for d, v in sorted(ledger["daily_closes"].items()) if d < cur_ny_day and v > 0]

    realized_vol_pct = target_vol_pct
    n_ret_days = 0
    if len(completed_items) >= 6:
        s_eq = pd.Series([v for _, v in completed_items], index=pd.to_datetime([d for d, _ in completed_items]))
        # Reindex across business days so flat days between trades count properly as 0.0% return days!
        b_idx = pd.bdate_range(start=s_eq.index[0], end=s_eq.index[-1])
        s_bday = s_eq.reindex(b_idx).ffill()
        rets = s_bday.pct_change().dropna().tail(20)
        n_ret_days = len(rets)
        if n_ret_days >= 5:
            sd = float(rets.std())
            # Scale by reference capital ratio if split-account mode is active
            if ref_cap and float(ref_cap) > 0:
                sd = sd * (mt5_equity / max(sizing_capital, 1.0))
            realized_vol_pct = max(1.0, sd * np.sqrt(252.0) * 100.0)

    drawdown_pct = max(0.0, (hwm_capital - sizing_capital) / max(hwm_capital, 1.0) * 100.0)

    if not enabled:
        return {
            "enabled": False,
            "capital_mode": cap_mode,
            "mt5_equity": round(mt5_equity, 2),
            "sizing_capital": round(sizing_capital, 2),
            "hwm_capital": round(hwm_capital, 2),
            "drawdown_pct": round(drawdown_pct, 2),
            "realized_vol_20d_pct": round(realized_vol_pct, 2),
            "vol_Days": n_ret_days,
            "base_risk_pct": base_risk_pct,
            "dd_mult": 1.0,
            "effective_risk_pct": base_risk_pct,
            "tier_label": "UN-GOVERNED",
        }

    vol_ratio_risk = float(np.clip(target_vol_pct / max(realized_vol_pct, 1e-4), min_vol_risk, max_vol_risk))
    vol_capped_risk = min(base_risk_pct, vol_ratio_risk)

    if drawdown_pct >= dd_t2_pct:
        dd_mult = dd_t2_mult
        tier_lbl = f"DD_TIER2 (-{drawdown_pct:.1f}% >= {dd_t2_pct:.0f}% -> {dd_t2_mult:.2f}x)"
    elif drawdown_pct >= dd_t1_pct:
        dd_mult = dd_t1_mult
        tier_lbl = f"DD_TIER1 (-{drawdown_pct:.1f}% >= {dd_t1_pct:.0f}% -> {dd_t1_mult:.2f}x)"
    elif vol_capped_risk < base_risk_pct - 0.01:
        dd_mult = 1.0
        tier_lbl = f"VOL_THROTTLED (20dVol {realized_vol_pct:.1f}% > {target_vol_pct:.0f}%)"
    else:
        dd_mult = 1.0
        tier_lbl = "NORMAL (10% Vol Target Clear)"

    eff_risk_pct = round(vol_capped_risk * dd_mult, 4)
    return {
        "enabled": True,
        "capital_mode": cap_mode,
        "mt5_equity": round(mt5_equity, 2),
        "sizing_capital": round(sizing_capital, 2),
        "hwm_capital": round(hwm_capital, 2),
        "drawdown_pct": round(drawdown_pct, 2),
        "realized_vol_20d_pct": round(realized_vol_pct, 2),
        "vol_days": n_ret_days,
        "base_risk_pct": base_risk_pct,
        "target_vol_pct": target_vol_pct,
        "dd_mult": dd_mult,
        "effective_risk_pct": eff_risk_pct,
        "tier_label": tier_lbl,
    }
